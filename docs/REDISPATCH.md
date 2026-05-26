# Hard-Failure Redispatch

When a task ends in a hard failure, agent_fleet can automatically retry it once (by default)
with a fresh agent and a structured handoff note that summarises what the failed attempt did.
The retry is called a **redispatch** — the same task spec, new session, new agent ID.

## Triggers: hard failures

The following conditions trigger a redispatch. They come from `redispatch._HARD_STATUSES`
plus any non-zero `exit_code`:

| Trigger | Source |
|---------|--------|
| `status = "error"` | Cursor SDK or internal infrastructure failure |
| `status = "cancelled"` | Task was externally cancelled |
| `status = "expired"` | Cursor agent session expired before completing |
| `status = "timeout"` | Fleet-level timeout (`timeout_seconds`) exceeded |
| `status = "scope_violation"` | Implementer modified files outside the persona allowlist |
| `status = "pipeline_nonzero"` | A phase's underlying command returned non-zero |
| `exit_code != 0` | Any result where `exit_code` is non-zero |

These all have one thing in common: the failure is **environmental or infrastructural**, not
a judgment call. Retrying with fresh context is likely to help.

## What does NOT trigger redispatch

| Outcome | Why it doesn't trigger |
|---------|----------------------|
| `verify_failed` | The agent's code is wrong — tests fail. Retrying the same agent with the same approach won't fix the logic. The output needs human review or a different persona. |
| `review_rejected` / `review_changes_requested` | The reviewer found quality issues. Same reasoning: the agent produced the output; redispatching it doesn't change the quality. |
| `review_blocked` | Reviewer issued a hard block. Needs human intervention. |

Soft failures return to the caller as-is. The `auto_fix` loop in the `code_review` pipeline
handles `verify_failed` and `review_changes_requested` separately (re-running a fix persona,
not re-running the same persona from scratch).

## Handoff shape

When a hard failure triggers a redispatch, `_extract_handoff()` produces a `HandoffNote` that
is prepended to the planner prompt of the next attempt.

`HandoffNote.render()` output example — attempt 2 of a task where the agent expired mid-run:

```
PREVIOUS ATTEMPT CONTEXT — read carefully before planning.
Failure mode: expired
Files modified before reset: src/a.py
Last stderr (truncated): Cursor send status: expired
Summary: Previous attempt ended with status='expired'. Modified 1 file(s) before reset. Do not repeat the same approach blindly; analyze the stderr above and plan around the failure mode.
```

For a chained second redispatch (attempt 3):

```
PREVIOUS ATTEMPT CONTEXT — read carefully before planning.
Failure mode: timeout
Files modified before reset: src/b.py, tests/test_b.py
Last stderr (truncated): Cursor run timed out after 900s
Summary: Previous attempt ended with status='timeout'. Modified 2 file(s) before reset. Do not repeat the same approach blindly; analyze the stderr above and plan around the failure mode.

(This is attempt #3; prior attempts also failed.)
```

The `files_touched` list captures files the failed attempt modified before the worktree was
reset. The planner sees these and can choose a different, faster approach (e.g. avoid
regenerating large files that triggered the timeout).

## Budget tuning

### Config

```yaml
# fleet.yaml
max_redispatches: 1   # 0 = disabled; 1 = default; 2-3 for flaky Cursor periods
```

`max_redispatches: 1` means one automatic retry — two total attempts. `max_redispatches: 0`
disables the loop entirely; any hard failure is returned immediately (useful in CI where you
want a clean signal).

### CLI override

The `--max-redispatches N` flag overrides the YAML value for a single run:

```bash
# CI: fail immediately on any hard failure
agent-fleet run "Add cache layer" \
  --workspace /path/to/repo \
  --max-redispatches 0

# Flaky Cursor period: allow two retries
agent-fleet run "Refactor auth module" \
  --workspace /path/to/repo \
  --max-redispatches 2
```

### Recommendations

| Scenario | Recommended `max_redispatches` |
|----------|-------------------------------|
| Normal development, default | `1` |
| Cursor platform is having a slow/flaky period | `2` or `3` |
| CI pipeline (GitHub Actions, etc.) | `0` — fail fast, let CI retry the run if needed |
| Tasks with large context windows (likely to expire) | `2` with short, focused goals |

Two hard failures in a row almost always mean the task spec is wrong or the goal is too broad.
Raising the budget above 3 rarely helps and wastes tokens and time.

### Visibility

Each redispatch attempt emits a `redispatch.attempt` event via the `on_event` callback with
the attempt number and whether a handoff is present:

```python
{"attempt": 1, "has_handoff": True}
```

The `FleetTaskResult` returned to the caller reflects the final attempt's outcome, so you can
tell a task succeeded on the second attempt by checking `result.status == "completed"` after
a run that would otherwise have failed.

## Worktree retention vs auto cleanup

Parallel and `use_worktree: true` runs create an isolated git worktree under
`worktree_base` (default `/tmp/agent-fleet-worktrees`). When the task finishes,
`TaskWorkspace.teardown(keep=...)` either removes that directory or leaves it for harvest.

| Mechanism | Behavior |
|-----------|----------|
| **`teardown(keep=False)`** (auto cleanup) | Default when the worktree should not survive. Runs `git worktree remove` and deletes the checkout directory. |
| **`teardown(keep=True)`** | Skips removal; the path stays on disk for `agent-fleet workstream harvest` or manual inspection. |
| **`should_keep_task_worktree()`** | Central policy used by the dispatcher to choose `keep=`. Keeps on `completed` / `merged`, when `code_review.auto_push` needs an isolated branch, and on recoverable soft failures (`verify_failed`, `review_changes_requested`) **when there are changed files**. |

Recoverable statuses are **not** redispatched (see table above). When they have local
changes, the dispatcher auto-commits before teardown so WIP is on the fleet branch even if
the worktree directory is kept:

```python
maybe_commit_recoverable_worktree(task_workspace, status, goal=task.goal)
task_workspace.teardown(keep=should_keep_task_worktree(...))
```

Hard failures (`error`, `timeout`, `scope_violation`, …) always use auto cleanup
(`keep=False`) so the next redispatch attempt starts from a fresh worktree.
