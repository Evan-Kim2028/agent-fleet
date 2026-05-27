# Phase 3 — Label-driven backlog dispatcher

## Symptom

When the fleet drains, nothing happens until the user posts a `/agent --persona X` comment by hand. With a sized backlog, this is a human-bottleneck-by-design.

## Design

A new module reads GitHub issues labeled `fleet-ready`, picks a budget-respecting subset, and **posts `/agent --persona X` comments**. The watcher's existing comment-trigger path picks those comments up on its next 30 s poll and dispatches them. The dispatcher writes no state of its own.

This is the `separate-before-serializing-shared-state` principle directly applied. The dispatcher and the watcher both have legitimate need to know what is in flight. Rather than coordinate via a new lock on `.agent-fleet-state.json`, the dispatcher uses GitHub comments as the message bus and lets the watcher remain the sole writer of `in_flight`.

### New module: `agent_fleet/issue_loop/backlog_dispatcher.py`

Class `BacklogDispatcher` with a single public method `dispatch_once(now: datetime) -> DispatchTickResult`.

```
BacklogDispatcher(
  repo: TargetRepo,
  capacity: FleetCapacityGate,
  state_path: Path,
  label: str = "fleet-ready",
  persona_label_prefix: str = "fleet-persona/",
  default_persona: str = "data",
)
```

### `dispatch_once` algorithm

1. Load current `.agent-fleet-state.json` (read-only; the watcher still owns writes).
2. List open issues with the `fleet-ready` label via `gh issue list --repo <repo> --label fleet-ready --state open --json number,labels,title`.
3. For each issue, determine eligibility (cheap checks first):
   - **Skip if** the issue number is in `in_flight` (live PIDs only — reap dead ones first via `reap_in_flight`).
   - **Skip if** any open PR exists with branch matching `agent-fleet/<persona>/<issue>` (GitHub query: `gh pr list --search "head:agent-fleet/..." --state open`).
   - **Skip if** the issue has the `agent-running/<issue>` mutex label (set by `dispatch.py:66-71`).
4. For each remaining issue, pick the persona:
   - First label matching `fleet-persona/<X>` → `X`.
   - Else `default_persona`.
5. Ask `FleetCapacityGate.try_admit(persona=...)` whether the dispatch fits the per-persona, per-issue, fleet-wide budget. Stop iterating when the gate refuses.
6. For each admitted issue, `gh issue comment <num> --body '/agent --persona <X> <!-- backlog-dispatcher -->'`. The trailing HTML comment is the marker the dispatcher uses on the *next* tick to know "we already asked; if the watcher hasn't dispatched, leave it alone for one more tick."
7. Return `DispatchTickResult(considered=N, skipped_for_reason={...}, dispatched=[(issue_num, persona), ...])` for logging.

### Idempotency

`make-operations-idempotent` applies. If the dispatcher runs twice in 30 s before the watcher's first poll, it would re-post the comment. Mitigation: the marker comment check. The eligibility step adds:

- **Skip if** the issue has a comment containing `<!-- backlog-dispatcher -->` within the last 5 minutes (`gh issue view --comments`, filtered client-side).

This bounds re-posts to one every 5 minutes per issue worst case, even with a crashing dispatcher.

### Wiring into `CombinedWatcher`

`agent_fleet/issue_loop/watcher.py:302-318` already has the polling loop. Add a `BacklogDispatcher` per target, tick interval default 10 min (configurable per target). On each tick:

```python
result = backlog_dispatcher.dispatch_once(now)
fleet_log.emit("backlog.tick", **asdict(result))
```

The watcher already has a per-target rate limiter; reuse it.

### Config

New section in `targets/<repo>.yaml`:

```yaml
backlog_dispatcher:
  enabled: true
  label: fleet-ready
  persona_label_prefix: fleet-persona/
  default_persona: data
  tick_interval_s: 600
```

Default `enabled: false` for safety. Operator opts in per target.

### Files touched

| File | Change |
|---|---|
| `agent_fleet/issue_loop/backlog_dispatcher.py` | New module. |
| `agent_fleet/issue_loop/watcher.py` | At `CombinedWatcher.poll_once`, run each target's backlog dispatcher tick. |
| `agent_fleet/issue_loop/config.py` | Parse the new `backlog_dispatcher` section. |
| `tests/test_backlog_dispatcher.py` | New. Mock `gh` responses; assert eligibility skip reasons; assert idempotent re-tick. |
| `targets/glassmarkets-silphcoanalytics.yaml` (or equivalent) | Opt-in with `enabled: true`. |

## Verification

### Static

```
cd /home/evan/Documents/agent-fleet
uv run pytest -q tests/test_backlog_dispatcher.py
```

### Runtime

Apply the `fleet-ready` label to two issues (say #1736 and #1702). Restart the watcher with the new config:

```
gh issue edit 1736 --repo glassmarkets/silphcoanalytics --add-label fleet-ready
gh issue edit 1702 --repo glassmarkets/silphcoanalytics --add-label fleet-ready
systemctl --user restart agent-fleet-watcher  # or equivalent
tail -F ~/.agent-fleet/logs/watch.log
```

Acceptance: within 10 minutes, `backlog.tick` span shows `dispatched=[(1736, data), (1702, data)]` (assuming capacity has room for both). Within 11 minutes, watcher logs show two dispatches kicked off.

Then immediately remove the label from one issue and re-add it; verify the dispatcher does NOT re-comment because in_flight reflects the running dispatch.

### Live regression

The watcher's comment-trigger path must remain untouched by phase 3. Confirm by manually posting a `/agent` comment on a non-labeled issue and verifying it dispatches as before.

## Rollback

Set `backlog_dispatcher.enabled: false` in the target config and restart the watcher. The module stays in the tree; the runtime behavior reverts.
