# Phase 4 — Main-drift detection in pr_loop

## Symptom

PR #2010 (silphcoanalytics issue #1399) generated commits for ~3 hours against a `pipeline/src/build_gold_sales_facts.py` shape that main had moved past via PR #2012 (compact parquet, `id_confidence` direction changed from `Float64` to `Utf8`). The conflict surfaced at merge time:

```
pipeline/tests/test_build_gold_sales_facts.py:102
<<<<<<< HEAD
    assert out.schema["id_confidence"] == pl.Float64
=======
    assert out.schema["id_confidence"] == pl.Utf8  # projected to Utf8 in unified schema
>>>>>>> origin/main
```

The acceptance criteria itself had become stale — not a fixable conflict. `pr_loop` had no view into this until the merge gate hit DIRTY.

## What pr_loop does today

`agent_fleet/pr_loop/github_ops.py:578-582` — `_sync_branch_before_push` rebases the worktree against `origin/<branch>`, never against `origin/main`. `merge_pr` at `:367-395` reads `mergeStateStatus`; for `BEHIND` it calls `gh pr update-branch` (which merges main into the PR branch); for `DIRTY` it does nothing.

So drift goes undetected until the very last step. The fix is to check drift at the *start* of every lifecycle cycle, not at merge time.

## Design

Add a drift-check at the top of each `lifecycle.run_cycle` iteration in `agent_fleet/pr_loop/lifecycle.py`.

### Algorithm

At cycle start, after refreshing the worktree (`checkout_branch`):

1. `git fetch origin main` (in the PR worktree).
2. `git merge-base --is-ancestor origin/main HEAD` — if true, no drift; continue.
3. Else, `git merge --no-commit --no-ff origin/main` in a scratch state:
   - Use `git merge-tree origin/main HEAD` to detect conflicts without touching the working tree. Simpler and reversible.
4. If `merge-tree` reports no conflict markers, drift exists but is auto-mergeable. Run `gh pr update-branch` (the existing BEHIND path) and continue the cycle.
5. If `merge-tree` reports conflict markers, drift is **unresolvable in this loop**:
   - **Comment** on the PR with: which files conflicted, the main commit SHA of the conflicting merge base, and a one-line explanation that the PR's premise has changed.
   - **Close** the PR with `gh pr close --delete-branch=false`.
   - **Reopen** the source issue (parse `agent_fleet/<persona>/<issue>` from the branch name) with `gh issue reopen <num>` if it was closed, and post a comment naming the conflict and asking the next dispatcher to replan.
   - Return `LifecycleResult("drift", "main moved out from under PR")`.

### Idempotency

`make-operations-idempotent` applies hard here. The lifecycle can re-enter; the close/reopen must not double-fire.

- The PR comment carries a marker `<!-- pr_loop:drift-detected -->`. Before posting, check existing comments; skip if marker already present in the last 24 h.
- `gh pr close` on an already-closed PR is a no-op (returns success). Safe.
- `gh issue reopen` on an open issue is a no-op. Safe.
- The issue replan comment carries `<!-- pr_loop:replan -->` and is similarly de-duped against the last 24 h.

### Files touched

| File | Change |
|---|---|
| `agent_fleet/pr_loop/lifecycle.py` | Add `_detect_drift(ctx)` called at `run_cycle` top. New `LifecycleResult("drift", ...)` outcome. |
| `agent_fleet/pr_loop/github_ops.py` | Add `merge_tree_against(base: str) -> MergeTreeResult` helper. Add `comment_pr(num, body, marker)` and `close_pr(num)` if not already present. |
| `agent_fleet/pr_loop/config.py` | Add `drift_check: bool = True` (default on; safe because the read path is just `git merge-tree`). |
| `tests/test_pr_loop_drift.py` | New. Stub `git merge-tree` output for no-drift, auto-mergeable-drift, and conflict cases. Assert the close+reopen happens exactly once per cycle and is idempotent across cycles. |

## Verification

### Static

```
cd /home/evan/Documents/agent-fleet
uv run pytest -q tests/test_pr_loop_drift.py
```

### Runtime

Recreate the PR #2010 scenario at small scale:

1. Branch off a stable commit on silphcoanalytics, open a fleet PR.
2. Land a conflicting change on main directly (test target).
3. Wait one pr_loop cycle on the fleet PR.

Acceptance:
- The PR receives one comment with the conflict file list and the `<!-- pr_loop:drift-detected -->` marker.
- The PR is closed.
- The source issue is reopened with a replan comment.
- A second pr_loop cycle on the same PR (still closed) does NOT re-comment or re-close.

### Live regression

The non-drift path must remain fast. Time `lifecycle.run_cycle` before and after; the new drift check adds one `git fetch` and one `git merge-tree`, expected ~1-2 s overhead per cycle. If observed overhead is >5 s, gate the check to once per N cycles.

## Open question

Cursor agents could in principle resolve simple conflicts. Worth a follow-up PR after this one lands: if `git merge-tree` reports a low-complexity conflict (e.g., only `pyproject.toml` version bump), spawn a one-shot cursor session to resolve it. Out of scope for this PR — the conservative close-and-reopen is the right baseline.

## Rollback

Set `drift_check: false` in the target's pr_loop config and restart the watcher. Or single revert.
