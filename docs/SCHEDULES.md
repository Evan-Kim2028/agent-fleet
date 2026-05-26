# Scheduled fleet dispatch

Run fleet jobs on a cron schedule from `.agent-fleet.yaml`. Schedules are
evaluated by `agent-fleet-watch` (combined watcher) or manually via
`agent-fleet-schedule`.

## Quick example

Standing GitHub issue `#42` for documentation maintenance:

```yaml
schedules:
  enabled: true
  poll_interval_s: 60
  jobs:
    - id: docs-daily
      cron: "0 6 * * *"
      timezone: America/New_York
      dispatch:
        kind: issue
        issue: 42
        persona: docs
        note: |
          Compare api/ against docs/. Update drift. PR only if substantive.
      policy:
        skip_if_in_flight: true
        missed: skip
        min_interval_s: 3600
```

Headless task (no GitHub issue — like `agent-fleet run`):

```yaml
    - id: dependency-audit
      cron: "0 9 * * 1"
      timezone: UTC
      dispatch:
        kind: task
        goal: "Audit outdated dependencies"
        persona: backend
        pipeline: code_review
        context: "Report only; do not bump versions."
```

## Dispatch kinds

| Kind | Behavior |
|------|----------|
| `issue` | Spawns `agent-fleet-issue-dispatch` — full pipeline, PR, issue comments |
| `task` | Spawns headless `FleetDispatcher` run in a subprocess |

`issue` kind requires `issue_dispatch` settings (comment marker, labels) in
`.agent-fleet.yaml`. Enable `issue_dispatch.enabled: true` or at minimum define
the comment marker fields the dispatch subprocess expects.

## CLI

```bash
# List jobs and next due times
agent-fleet-schedule list --workspace /path/to/repo

# Evaluate all schedules once (also: agent-fleet-watch --once)
agent-fleet-schedule tick --workspace /path/to/repo

# Manual fire (ignores cron expression)
agent-fleet-schedule run --id docs-daily --workspace /path/to/repo
```

## Watcher integration

When `schedules.enabled: true`, the combined watcher (`agent-fleet-watch`)
evaluates schedules on every poll cycle alongside issue comments and PR loop
work. You can enable schedules without issue comment dispatch.

State is stored in unified `.agent-fleet-state.json` under the `schedules` key.

## External cron alternative

For minimal setup before upgrading, system cron can call:

```bash
agent-fleet-schedule tick --workspace /path/to/repo
```

Run every minute; the schedule module handles dedup and next-fire tracking.

## Policy

| Field | Default | Meaning |
|-------|---------|---------|
| `skip_if_in_flight` | `true` | Skip if this job or target issue already has a live dispatch |
| `missed` | `skip` | `skip`, `catch_up_once`, or `catch_up_all` when host was down |
| `min_interval_s` | `0` | Hard minimum seconds between fires |

## Systemd

Use the existing `agent-fleet-watch` unit — schedules piggyback on the same
daemon. Set `poll_interval_s` on `schedules` (default 60s) and/or
`issue_dispatch.poll_interval_s`; the watcher uses the maximum of enabled loops.
