# Phase 2 — Verify and harden auto-merge

## Symptom

User has had to `gh pr merge --squash` clean fleet PRs by hand for two driving sessions. The expectation per the original four-PR ask was that `pr_loop` would auto-merge on green.

## What is actually true

`pr_loop` already has the wiring.

- `agent_fleet/pr_loop/config.py:30` — `auto_merge: bool = True` by default.
- `agent_fleet/pr_loop/config.py:65` — same default when read from YAML.
- `agent_fleet/pr_loop/lifecycle.py:828-881`:
  - Line 828: `while True:` loop polling CI.
  - Line 830: `wait_for_ci_green(ci_poll_s=10s)`.
  - Line 835: break on green.
  - Line 876: `if not loop_config.auto_merge: return LifecycleResult("ready", ...)`. We do not hit this branch with the default.
  - Line 881: `try_merge(...)`.
- `agent_fleet/pr_loop/github_ops.py:367-395` — `merge_pr` calls `gh pr merge --squash` (immediate), polls `MERGED` state up to 95 s. If `mergeStateStatus == BEHIND`, calls `gh pr update-branch`. If `DIRTY` — no handling. If `CLEAN` and squash succeeds — returns success.

So either the lifecycle never reached `try_merge`, or `try_merge` returned a non-success and nothing retried.

## Investigation step (do before writing code)

Read `~/.agent-fleet/logs/watch.log` and `~/.agent-fleet/traces/spans-2026-05-27.jsonl` for the most recent fleet PR that went green and was not merged. Grep for `pr_loop.merge`, `pr_loop.ready`, `try_merge`. Three plausible outcomes:

1. `pr_loop.ready` emitted with `auto_merge=False`: someone set the YAML to `false` for that target. Fix: revert the YAML override; no code change.
2. `try_merge` emitted `mergeStateStatus=DIRTY` and stopped: PR 4 (main-drift) covers this. No PR 2 code change needed; PR 2 collapses into a comment in `lifecycle.py` noting "DIRTY handled by phase 4."
3. `try_merge` succeeded per the log but the PR is still open per `gh pr view`: GitHub API lag or a polling bug. Investigate `merge_pr`'s 95 s timeout; extend or add a retry.
4. The CI poll never breaks: `wait_for_ci_green` may be returning a sentinel that the break at `:835` doesn't recognize. Read the code at `:830-835` against the actual log.

Pick the actual outcome before writing code. The scoping question is: is there a code defect, or did this PR's premise dissolve under inspection?

## If outcome (1) — no code change

Edit the relevant target config YAML in `~/.agent-fleet/` or `targets/`, set `auto_merge: true`. No PR.

## If outcome (2) — defer to PR 4 entirely

Open PR 2 as a small comment-only diff in `lifecycle.py:828` documenting that DIRTY is the responsibility of phase 4's drift detector. Or skip PR 2.

## If outcome (3) or (4) — code change

### Outcome 3 design

`github_ops.py:367-395` polls `MERGED` state. If the poll loop hits its 95 s ceiling but GitHub reports `state != MERGED`, log the actual mergeable/mergeStateStatus pair and return failure. Lifecycle at `:881` checks the return and re-tries `try_merge` once after a 30 s sleep, capped at 3 attempts per lifecycle cycle.

Idempotent: `gh pr merge --squash` on an already-merged PR returns success or a benign "already merged" error; the retry path tolerates both.

### Outcome 4 design

Read `pr_loop/ci.py` (or wherever `wait_for_ci_green` lives) to confirm the sentinel returned on green. If the comparison at `lifecycle.py:835` is `== "green"` and the helper returns an enum or a different string, fix the comparison. This is a one-line fix; the test is `tests/test_pr_loop_lifecycle.py` with a stub `wait_for_ci_green`.

## Files touched (worst case)

| File | Change |
|---|---|
| `agent_fleet/pr_loop/github_ops.py` | At `merge_pr`, distinguish poll-timeout-but-CLEAN from poll-timeout-with-different-state. Log both. |
| `agent_fleet/pr_loop/lifecycle.py` | At `:881`, retry `try_merge` up to 3 times with backoff if the return is `(False, reason="poll_timeout")`. |
| `tests/test_pr_loop_lifecycle.py` | Add test for the retry path with a stubbed `merge_pr` that returns timeout then success. |

## Verification

### Static

```
cd /home/evan/Documents/agent-fleet
uv run pytest -q tests/test_pr_loop_lifecycle.py
```

### Runtime

After phase 1 ships and the planner stops dying, dispatch one issue end-to-end:

```
gh issue comment 1736 --repo glassmarkets/silphcoanalytics --body '/agent --persona data'
```

Watch `~/.agent-fleet/logs/watch.log` for the PR open, CI green, and `pr_loop.merge` success spans. Acceptance: PR transitions from open → merged with zero human action.

## Rollback

Single revert. The retry loop is bounded so a regression is bounded too.
