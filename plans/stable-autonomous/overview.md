# Stable Autonomous Fleet — Plan Overview

## Why now

The fleet driving silphcoanalytics requires constant human babysitting. Three failure modes burn the most attention:

1. The PLAN phase fails with the message `ValueError: No JSON object found in LLM output`. The message has zero diagnostic content. The dispatch logs at `dispatch-1517-retry2.log` and `dispatch-1690.log` show identical lines at second-resolution timestamps — the underlying cursor call clearly failed in some structured way, but `CursorSession.send` swallowed the exception and returned empty stdout. The planner saw empty stdout and raised the generic "no JSON" error. Hours of monitor ticks have been spent guessing whether it was a transient API failure, an auth issue, or a config drift; the swallowed exception cost all of that time. Watcher-driven dispatch happens to work because the watcher always sets `AGENT_FLEET_TARGET_CONFIG`; manual `python -m agent_fleet.issue_loop.dispatch` does not, and `resolve_repo_config` walks back to a different config — silently.

2. Fleet PRs go green, then sit. `pr_loop/config.py:30` has `auto_merge: bool = True` and `lifecycle.py:876` reads it before calling `try_merge`. So PRs *should* merge on green. They have not been. Either the gate is being missed at the right wall-clock window, or `try_merge` itself returns silently on a non-clean mergeable state and never retries. Verification: the merged-state lookup uses `BEHIND` and triggers `update-branch`; `DIRTY` (the PR #2010 case) has no path; `CLEAN` should fall through and succeed. Need a runtime trace before deciding what code to change.

3. The fleet drains and there is nothing to do. The watcher only reacts to `/agent --persona X` comments. With a 200-issue backlog labeled with intent, requiring the user to comment by hand is a designed-in human bottleneck.

A fourth failure mode is rarer but bites hard when it happens: main moves out from under an open fleet PR. PR #2010 spent ~3 hours generating commits against a `pipeline/src/build_gold_sales_facts.py` shape that main had already moved past. By the time the conflict surfaced, the PR's acceptance criteria were stale. `pr_loop/github_ops.py:582` rebases against `origin/<branch>` only — never against `origin/main`.

## Scope

Four PRs to `agent-fleet`. Not silphcoanalytics. Not test infrastructure rewrites. Not observability additions beyond what the planner-bug fix forces.

| # | Title | Estimate | Blast radius |
|---|---|---|---|
| 1 | Surface real planner errors | ~2 hr | All callers of `CursorSession.send`; gated behind keeping legacy result shape for now |
| 2 | Verify and harden auto-merge | ~30 min | `pr_loop/lifecycle.py` merge gate; likely diagnostic-only |
| 3 | Label-driven backlog dispatcher | ~2 hr | New module + `CombinedWatcher` tick; posts comments only |
| 4 | Main-drift detection in pr_loop | ~2 hr | `pr_loop/lifecycle.py` cycle start; close-and-reopen path |

PRs ship in order. Each is mergeable on its own. PR 1 ships first because every other PR's diagnostics depend on the planner failing loudly.

## Out of scope

- Any silphcoanalytics-side change. The driving target is just a witness — bugs are in agent-fleet.
- Rewriting test infrastructure or adding shared fakes. Existing tests use local `FakeBackend` and per-test monkeypatches; that pattern stays.
- Branch protection on silphcoanalytics. The repo is on the free plan; `gh pr merge --auto` is a no-op there. Designs must not depend on it.
- New shared on-disk state. The label-driven dispatcher writes nothing the watcher doesn't already write.
- Stopping the in-flight #1690 task agents. They are running in `/tmp/agent-worktrees/task-0-8d147344` and `/tmp/agent-worktrees/task-1-6b580255` and will produce a PR or fail on their own.

## Constraints

- Main Claude session is Opus. Per `/home/evan/.claude/CLAUDE.md`, every `Agent` tool call must pass `model: "sonnet"` or `model: "haiku"`. No Opus subagents from this session.
- The live watcher in `~/.agent-fleet/logs/watch.log` must stay up during the rollout. Each PR's runtime change is gated so a deploy that needs the watcher to restart is its own visible step.
- No commit with `--no-verify`, `--no-gpg-sign`, or `-i`. No `--amend`. No force-push to `main`. Commit messages via HEREDOC, trailer line `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`.
- `.env` files at `/home/evan/Documents/silphcoanalytics/.env` and `/home/evan/.agent-fleet/.env` (mode 0600). Never staged, never printed.
- `CURSOR_API_KEY` never echoed.
- `AGENT_FLEET_TARGET_CONFIG` env var is the contract between watcher and dispatch. Any change to dispatch's config-resolution path is breaking for the watcher path and must be tested against the watcher fixture.

## Principles applied

- **fix-root-causes.** PR 1's root cause is not "the planner is too strict"; it is "the boundary swallows the diagnostic." Don't add fallback prose to the planner — strip the swallowing instead.
- **boundary-discipline.** `CursorSession.send` is the boundary. It is the place to surface real failures, not the planner. Per-caller error handling stays at the call sites where the caller can decide.
- **migrate-callers-then-delete-legacy-apis.** `CursorLLMResult` has nine callers. If we change `send` to raise, every caller must move in one wave. The PR-1 design keeps the result shape and adds a `cause` field so callers migrate optionally; PR 1 itself only migrates the planner.
- **separate-before-serializing-shared-state.** PR 3's dispatcher and the watcher both touch `.agent-fleet-state.json`. The design has the dispatcher write nothing to that file — it posts a comment, the watcher's existing path picks it up 30 s later, the watcher writes `in_flight`. No new shared state and no new lock.
- **make-operations-idempotent.** PR 3 must not double-dispatch if it runs every 10 minutes and the watcher is also active. The gate keys are: `agent-running/<issue>` mutex label, open fleet PR for the issue, presence in `in_flight`. PR 4 must not double-close an already-closed PR or double-comment a "drift detected" note.
- **subtract-before-you-add.** PR 2 may turn out to be diagnostic-only. If the verification step in `phase-2-auto-merge.md` finds `auto_merge` already works end-to-end and the issue was just specific PRs in DIRTY state (which PR 4 handles), PR 2 collapses into a one-line log change.
- **foundational-thinking.** Sequencing: PR 1 first, because PRs 2-4 depend on being able to read what failed. PR 4 last, because the close-and-reopen path needs PR 1's diagnostics to write a useful replan note on the reopened issue.

## Alternatives considered

- **PR 1 alternative: leave `CursorSession.send` as-is, only fix the planner.** Rejected. The planner is one of nine callers; the others (`researcher`, `reviewer`, `tech_lead`, `synthesizer`) read only stdout too and will hit the same opaque failure. Fixing one caller leaves a known landmine.
- **PR 1 alternative: change `send` to raise on failure.** Rejected for this PR. Two callers (`implementer`, `fleet_scope`) already inspect `exit_code` and would need migration. Doable but doubles PR 1's blast radius. Deferred to a follow-up after PR 1's `cause` field lands.
- **PR 2 alternative: re-implement merge as a periodic sweep.** Rejected. The lifecycle gate is the right place; if it is silently failing, the answer is to log why, not to add a parallel sweeper that races it.
- **PR 3 alternative: dispatcher writes `in_flight` directly.** Rejected per `separate-before-serializing-shared-state`. Two writers to one JSON file is what the existing lock was added to manage; adding a third introduces a new contention surface for no benefit.
- **PR 4 alternative: try to auto-rebase against `main` and resolve conflicts.** Rejected. Cursor agents can resolve conflicts but the time-to-resolution is unbounded; close-and-reopen with a replan note is the conservative move and preserves an audit trail.

## Verification

After every PR, all of these must pass from `/home/evan/Documents/agent-fleet`:

```
uv run ruff check .
uv run pytest -q
```

Per-PR runtime verification is in each phase file. The end-state acceptance is one full autonomous cycle:

1. A `fleet-ready` labeled issue gets a `/agent` comment from PR 3.
2. The watcher dispatches it (existing path).
3. PR opens, CI goes green, `try_merge` lands it (PR 2, possibly diagnostic-only).
4. If main drifted mid-cycle, the PR is closed and the issue reopened with a replan note (PR 4).
5. No manual intervention for 30 minutes. Logged in `~/.agent-fleet/logs/watch.log`.

If step 5 fails, the plan failed even if every individual PR's tests pass.

## Files

- `phase-1-planner-bug.md`
- `phase-2-auto-merge.md`
- `phase-3-auto-dispatch.md`
- `phase-4-main-drift.md`
- `testing.md`
