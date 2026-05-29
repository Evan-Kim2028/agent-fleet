"""Schema guard for the main fleet event stream.

Two JSONL streams coexist under the runs directory: ``<run_id>.jsonl`` (the
strict FleetEvent stream) and ``<run_id>.bridge.jsonl`` (raw SDK passthrough
where ``event`` is a dict). This test pins the strict-stream contract so a
future change that widens ``FleetEvent.event`` or routes bridge records into
the main sink will fail loudly. See ``docs/OBSERVABILITY.md``.
"""

from __future__ import annotations

import json
import typing
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.observability.context import bind_run
from agent_fleet.observability.events import FleetEvent
from agent_fleet.observability.log import RunLog
from agent_fleet.observability.sinks import JsonlFileSink


def test_fleet_event_field_is_typed_str() -> None:
    hints = typing.get_type_hints(FleetEvent)
    assert hints["event"] is str, (
        "FleetEvent.event must stay typed as `str`. Bridge passthrough "
        "(where event is a dict) belongs in <run_id>.bridge.jsonl."
    )


def test_run_log_emits_string_event(tmp_path: Path) -> None:
    run_log = RunLog.create(
        run_id="guard1",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    with bind_run(run_log, run_log.context):
        run_log.emit("phase.start", phase="PLAN", data={"items": 1})
        run_log.emit("fleet.task.complete", data={"status": "completed"})

    path = tmp_path / "guard1.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "RunLog produced no JSONL output"
    for raw in lines:
        record = json.loads(raw)
        assert isinstance(record["event"], str), (
            f"main-stream event must be a string, got {type(record['event']).__name__}: {record!r}"
        )
        assert "." in record["event"] or record["event"].isidentifier(), (
            f"event name should be a dotted-namespace identifier, got {record['event']!r}"
        )


def test_jsonl_sink_rejects_no_extra_keys(tmp_path: Path) -> None:
    """Sanity check that JsonlFileSink only serializes FleetEvent.to_dict() keys."""
    sink = JsonlFileSink(tmp_path / "g2.jsonl")
    event = FleetEvent.now(run_id="g2", event="run.start", data={"title": "x"})
    sink.emit(event)

    record = json.loads((tmp_path / "g2.jsonl").read_text(encoding="utf-8").strip())
    allowed = {"ts", "run_id", "event", "level", "phase", "issue_number", "persona", "data"}
    extra = set(record) - allowed
    assert not extra, f"unexpected keys leaked into main stream: {extra}"
    assert isinstance(record["event"], str)


def test_live_main_stream_records_are_string_events() -> None:
    """Sweep ~/.agent-fleet/fleet/runs/*.jsonl (excluding *.bridge.jsonl).

    Skips when no runs dir exists. Caps to the 5 most recent files so a CI
    sandbox with a populated runs dir doesn't turn this into a multi-minute scan.
    """
    from agent_fleet.fleet_paths import default_runs_dir

    runs_dir = default_runs_dir()
    if not runs_dir.is_dir():
        pytest.skip(f"no runs dir at {runs_dir}")

    candidates = sorted(
        (p for p in runs_dir.glob("*.jsonl") if not p.name.endswith(".bridge.jsonl")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:5]
    if not candidates:
        pytest.skip(f"no main-stream files under {runs_dir}")

    for path in candidates:
        with path.open(encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"{path}:{lineno} invalid JSON: {exc}") from exc
                ev = record.get("event")
                assert isinstance(ev, str), (
                    f"{path}:{lineno} has non-string event: {type(ev).__name__} "
                    "(if this file is bridge passthrough it should end in .bridge.jsonl)"
                )
