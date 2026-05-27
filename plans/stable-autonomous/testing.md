# Testing matrix

| PR | Static (every PR) | Phase-specific unit tests | Runtime reproduction | Live regression check |
|---|---|---|---|---|
| 1 | `uv run ruff check .` + `uv run pytest -q` | `tests/test_planner.py` (rich error on `exit_code != 0`); `tests/test_cursor_session.py` (`cause` populated on swallow); `tests/test_dispatch.py` (exits 2 on missing env vars) | Unset `AGENT_FLEET_TARGET_CONFIG`, run `python -m agent_fleet.issue_loop.dispatch` for #1517; assert exit code 2 and named env var in stderr. Then set env vars + bad `CURSOR_API_KEY`; assert `PLAN cursor call failed: exit_code=...` with auth detail. | Comment `/agent --persona data` on a real issue; watcher dispatches; runner completes PLAN. |
| 2 | Same | `tests/test_pr_loop_lifecycle.py` (retry path with stubbed `merge_pr` timeout-then-success) | Dispatch one issue end-to-end, observe PR open → green → merged with zero human action. | Watcher merge spans show `pr_loop.merge` success for the next 3 fleet PRs. |
| 3 | Same | `tests/test_backlog_dispatcher.py` (skip reasons; capacity gating; marker-comment idempotency) | Label 2 issues `fleet-ready`; restart watcher; within 11 min, both dispatch. Re-toggle label; assert no double-dispatch. | Non-labeled issues still dispatch via existing comment-trigger path. |
| 4 | Same | `tests/test_pr_loop_drift.py` (no-drift, auto-mergeable, hard conflict; close/reopen idempotency across cycles) | Create artificial conflict against main; observe one drift comment, PR close, issue reopen; second cycle silent. | `lifecycle.run_cycle` runtime overhead from drift check ≤ 5 s. |

## End-to-end acceptance (all four PRs landed)

One full autonomous cycle with zero human intervention for 30 minutes:

1. A `fleet-ready` labeled issue gets a `/agent` comment from PR 3.
2. Watcher dispatches; runner PLAN/RESEARCH/IMPLEMENT completes.
3. PR opens, CI goes green, `try_merge` lands it (PR 2 retry path or vanilla `try_merge`).
4. If main drifted mid-cycle, PR is closed and issue reopened with replan note (PR 4).
5. `~/.agent-fleet/logs/watch.log` records the full cycle without manual intervention.

If step 5 fails — manual `/agent` comments or manual merges happen during the 30 min — the plan failed even if every per-PR test passes.

## Verification of plan itself

Before implementation starts, run a `figure-it-out`-style dry-run review:

- Confirm `auto_merge` is already `True` by default (it is — `pr_loop/config.py:30`).
- Confirm watcher's `AGENT_FLEET_TARGET_CONFIG` env injection (`watcher.py:150`).
- Confirm silphcoanalytics has no branch protection (confirmed: free plan, `gh pr merge --auto` is no-op).
- Confirm `dispatch.py` exit-1 path at `:53-55` (manual dispatch divergence cause).
- Confirm `pr_loop/github_ops.py:582` rebases against `origin/<branch>` only (no main rebase).

All five confirmed during exploration. The plan is grounded.
