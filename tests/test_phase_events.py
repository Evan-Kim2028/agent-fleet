"""phase.start / phase.end richer payload tests."""

from __future__ import annotations

from agent_fleet.observability.events import RunContext
from agent_fleet.observability.log import RunLog
from agent_fleet.observability.sinks import MemoryRingSink


def _make_log(run_id: str = "test-run-1") -> tuple[RunLog, MemoryRingSink]:
    sink = MemoryRingSink()
    log = RunLog(
        run_id=run_id,
        context=RunContext(run_id=run_id),
        sinks=[sink],
    )
    return log, sink


def test_phase_start_carries_phase_and_run_id() -> None:
    log, sink = _make_log("r-abc")
    with log.phase("SYNTHESIZE"):
        pass

    start_events = [e for e in sink.events if e.event == "phase.start"]
    assert len(start_events) == 1
    data = start_events[0].data
    assert data["phase"] == "SYNTHESIZE"
    assert data["run_id"] == "r-abc"


def test_phase_end_carries_wall_s_and_status_on_success() -> None:
    log, sink = _make_log("r-abc")
    with log.phase("IMPLEMENT"):
        pass

    end_events = [e for e in sink.events if e.event == "phase.end"]
    assert len(end_events) == 1
    data = end_events[0].data
    assert data["phase"] == "IMPLEMENT"
    assert data["run_id"] == "r-abc"
    assert isinstance(data["wall_s"], float)
    assert data["wall_s"] >= 0.0
    assert data["status"] == "completed"


def test_phase_end_carries_failed_status_on_exception() -> None:
    log, sink = _make_log("r-err")
    try:
        with log.phase("VERIFY"):
            raise ValueError("boom")
    except ValueError:
        pass

    end_events = [e for e in sink.events if e.event == "phase.end"]
    assert len(end_events) == 1
    assert end_events[0].data["status"] == "failed"


def test_phase_start_includes_extra_kwargs() -> None:
    log, sink = _make_log("r-extra")
    with log.phase("RESEARCH", items=3):
        pass

    start_events = [e for e in sink.events if e.event == "phase.start"]
    assert start_events[0].data["items"] == 3


def test_matching_run_id_across_start_and_end() -> None:
    """phase.start and phase.end must carry the same run_id and phase."""
    run_id = "r-match"
    log, sink = _make_log(run_id)
    with log.phase("REVIEW"):
        pass

    start = next(e for e in sink.events if e.event == "phase.start")
    end = next(e for e in sink.events if e.event == "phase.end")
    assert start.data["run_id"] == end.data["run_id"] == run_id
    assert start.data["phase"] == end.data["phase"] == "REVIEW"
