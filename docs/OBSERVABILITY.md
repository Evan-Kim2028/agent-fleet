# Observability

Agent-fleet writes two JSONL streams per run. They live side-by-side under the
runs directory but carry different schemas and serve different consumers.

## Runs directory

```
$AGENT_FLEET_RUNS_DIR              # default: ~/.agent-fleet/fleet/runs
├── <run_id>.jsonl                 # main fleet event stream
└── <run_id>.bridge.jsonl          # bridge passthrough stream (optional)
```

`<run_id>` is the same id surfaced in `FleetRunResult.run_id` and on every
`FleetEvent.run_id` field, so the two files can always be joined.

## Stream 1 — main fleet event stream (`<run_id>.jsonl`)

**Producer.** `RunLog.emit()` in `agent_fleet/observability/log.py`. Every fleet
component goes through this method.

**Sink.** `JsonlFileSink` in `agent_fleet/observability/sinks.py`.

**Record shape.** One JSON object per line, conforming to `FleetEvent.to_dict()`
in `agent_fleet/observability/events.py`:

```json
{
  "ts": "2026-05-29T13:55:00+00:00",
  "run_id": "...",
  "event": "phase.end",
  "level": "info",
  "phase": "implement",
  "issue_number": 42,
  "persona": "coder",
  "data": { ... }
}
```

`event` is **always a string** — a dotted-namespace event name. Free-form
payload goes under `data`. Optional fields (`phase`, `issue_number`, `persona`,
`data`) are omitted when empty.

**Canonical event names.** Authoritative list (extracted from `RunLog` and
`*.emit("…")` call sites):

| Namespace | Event | Emitted by |
| --- | --- | --- |
| Run lifecycle | `run.start`, `run.end`, `run.resume` | `RunLog`, `runner.py` |
| Phase lifecycle | `phase.start`, `phase.end` | `RunLog` |
| LLM usage | `llm.usage`, `llm.usage.task_rollup` | `RunLog` |
| Fleet dispatch | `fleet.task.complete`, `fleet.task.error`, `admission.denied` | `dispatcher.py`, `dispatcher_task.py` |
| Equip / MCP | `equip.resolved`, `mcp.required` | `runner.py` |
| Complexity | `complexity.ceiling_metric` | `phases.py` |
| Verify | `verify.bootstrap_error` | `code_review/loop.py` |
| Memory | `memory.snapshot` | `RunLog` |
| Orchestration | `orchestration.persona_generated` | `persona_foundry.py` |
| PR loop | `pr_loop.error`, `pr_loop.drift`, `pr_loop.review_fix.start`, `pr_loop.ci.wait`, `pr_loop.ci.green`, `pr_loop.ready`, `pr_loop.merge.attempt` | `pr_loop/lifecycle.py` |

When adding a new event, prefer extending an existing namespace over inventing
a one-off. The schema guard test (`tests/test_event_schema_guard.py`) enforces
that `FleetEvent.event` is always a string and that every emit through the
`RunLog` ends up as a parseable line in this stream.

## Stream 2 — bridge passthrough (`<run_id>.bridge.jsonl`)

**Producer.** `cursor_backend._write_bridge_event()` in
`agent_fleet/cursor_backend.py`.

**Sink.** Direct file append (not a `LogSink`). The bridge stream is a debug
side-channel; it is not part of the structured event taxonomy.

**Record shape.** Different from the main stream — `event` is a **dict**, not
a string:

```json
{
  "ts": "2026-05-29T13:55:00+00:00",
  "run_id": "...",
  "event": {
    "attribute_one": ...,
    "attribute_two": ...,
    ...
  }
}
```

The `event` dict is built by reflecting every public, non-callable attribute on
the SDK update object that the Claude Code bridge surfaces. The shape is
**defined by the upstream SDK**, not by agent-fleet, so the keys vary by SDK
version. Consumers must treat the dict as opaque and probe defensively.

**Why a separate file.** The bridge stream is high-volume, low-signal, and
schema-unstable. Mixing it into the main stream would (a) blow up the size of
the main JSONL, (b) force every downstream consumer to filter on the
`event`-is-string predicate, and (c) couple the fleet event schema to the
upstream SDK's evolving update objects. Keeping the two files separate lets the
main stream stay strictly typed and small while the bridge stream stays
fire-and-forget.

## Aggregating across runs

When walking the runs directory, treat the two streams differently:

```python
for path in runs_dir.glob("*.jsonl"):
    if path.name.endswith(".bridge.jsonl"):
        continue            # different schema
    for line in path.open():
        event = json.loads(line)
        # event["event"] is a string
```

`build_run_metrics()` in `agent_fleet/observability/run_metrics.py` is the
canonical rollup for a single task's outcome. Every production caller passes
the full kwarg set so the rollup carries `repo_key`, `issue_number`,
`duration_seconds`, `error`, `pr_number`, and `usage_rollup` whenever they are
known. As of v0.10.1 the four production call sites (`runner.py`,
`dispatcher.py`, `dispatcher_task.py`, `level_up/record.py`) all pass the same
shape, so the metric dict is comparable across the dispatcher path and the
single-task runner path.

## Where things live

| Concern | File |
| --- | --- |
| Event record type | `agent_fleet/observability/events.py` |
| Emit + per-run sink wiring | `agent_fleet/observability/log.py` |
| File and in-memory sinks | `agent_fleet/observability/sinks.py` |
| Per-task metrics rollup | `agent_fleet/observability/run_metrics.py` |
| Bridge passthrough writer | `agent_fleet/cursor_backend.py` (`_write_bridge_event`) |
| Runs dir resolution | `agent_fleet/fleet_paths.py` (`default_runs_dir`) |
| Schema guard test | `tests/test_event_schema_guard.py` |
