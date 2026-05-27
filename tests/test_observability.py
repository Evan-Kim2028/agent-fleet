"""Tests for structured fleet observability."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_fleet.observability.context import bind_run, get_run_log
from agent_fleet.observability.events import FleetEvent, RunContext
from agent_fleet.observability.log import RunLog
from agent_fleet.observability.sinks import JsonlFileSink, MemoryRingSink

if TYPE_CHECKING:
    from pathlib import Path


def test_fleet_event_roundtrip() -> None:
    event = FleetEvent.now(
        run_id="abc123",
        event="phase.start",
        phase="PLAN",
        data={"items": 2},
    )
    payload = json.loads(event.to_json())
    assert payload["run_id"] == "abc123"
    assert payload["event"] == "phase.start"
    assert payload["phase"] == "PLAN"
    assert payload["data"]["items"] == 2


def test_run_log_writes_jsonl(tmp_path: Path) -> None:
    run_log = RunLog.create(
        run_id="testrun1",
        issue_number=42,
        persona="frontend",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    with bind_run(run_log, run_log.context):
        run_log.emit("run.start", data={"title": "Example"})
        run_log.memory(available_ram_gb=32.0, playwright_mcp_processes=1)

    path = tmp_path / "testrun1.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "run.start"
    assert first["issue_number"] == 42


def test_bind_run_context_var() -> None:
    run_log = RunLog.create(
        run_id="ctx1",
        include_memory_ring=False,
    )
    assert get_run_log() is None
    with bind_run(run_log, run_log.context):
        assert get_run_log() is run_log


def test_memory_ring_sink() -> None:
    sink = MemoryRingSink(max_events=2)
    sink.emit(FleetEvent.now(run_id="r1", event="a"))
    sink.emit(FleetEvent.now(run_id="r1", event="b"))
    sink.emit(FleetEvent.now(run_id="r1", event="c"))
    assert [event.event for event in sink.events] == ["b", "c"]


def test_jsonl_sink_creates_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "run.jsonl"
    sink = JsonlFileSink(path)
    sink.emit(FleetEvent.now(run_id="nested", event="run.start"))
    assert path.is_file()


def test_run_context_fields() -> None:
    ctx = RunContext(run_id="x", issue_number=1, persona="backend", visual_audit=True)
    assert ctx.visual_audit is True
    assert ctx.issue_number == 1
