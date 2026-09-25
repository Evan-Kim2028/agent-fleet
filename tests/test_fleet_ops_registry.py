"""Lane registry: JSON snapshot per lane, append-only shared event stream."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_fleet.fleet_ops import registry
from agent_fleet.fleet_ops.registry import (
    STATE_APPROVED,
    STATE_RUNNING,
    LaneRecord,
    append_event,
    events_path,
    find_lane,
    iter_records,
    lane_state_path,
    lanes_dir,
    load_record,
    process_alive,
    process_starttime,
    read_events,
    save_record,
    update_record,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point agent_fleet_home() at tmp_path so the real ~/.agent-fleet is untouched."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "agent-fleet"))


def test_record_round_trips_through_disk() -> None:
    record = LaneRecord(
        lane="stampinplace",
        operator="documents-0e",
        state=STATE_RUNNING,
        engine="cmd",
        pr=3510,
        head="abc123def",
        pid=4242,
        pgid=4240,
        starttime=99,
        tool_calls=10,
        tool_errors=1,
    )
    path = save_record(record)
    assert path == lane_state_path("documents-0e", "stampinplace")

    loaded = load_record("documents-0e", "stampinplace")
    assert loaded is not None
    assert loaded.lane == "stampinplace"
    assert loaded.pr == 3510
    assert loaded.pid == 4242
    assert loaded.starttime == 99
    assert loaded.tool_errors == 1


def test_operators_get_separate_files_for_the_same_lane_name() -> None:
    """Two operators may legitimately own the same lane name."""
    save_record(LaneRecord(lane="shared", operator="documents-0e", pr=1))
    save_record(LaneRecord(lane="shared", operator="documents-1d", pr=2))

    zero_e = load_record("documents-0e", "shared")
    one_d = load_record("documents-1d", "shared")
    assert zero_e is not None
    assert one_d is not None
    assert zero_e.pr == 1
    assert one_d.pr == 2
    assert len(list(iter_records())) == 2


def test_events_are_append_only_and_never_rewritten() -> None:
    append_event("documents-0e", "l1", "lane.started")
    append_event("documents-0e", "l1", "lane.engine.done", exit_code=0)
    append_event("documents-1d", "l2", "lane.started")

    raw = events_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == 3
    kinds = [json.loads(line)["event"] for line in raw]
    assert kinds == ["lane.started", "lane.engine.done", "lane.started"]

    # Appending again must not disturb earlier lines.
    append_event("documents-0e", "l1", "lane.pr.guaranteed", pr=7)
    after = [json.loads(line) for line in events_path().read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in after[:3]] == kinds
    assert after[3]["pr"] == 7


def test_read_events_filters_by_operator_and_lane() -> None:
    append_event("documents-0e", "a", "x")
    append_event("documents-0e", "b", "y")
    append_event("documents-1d", "a", "z")

    assert len(read_events()) == 3
    assert len(read_events(operator="documents-0e")) == 2
    assert len(read_events(operator="documents-0e", lane="a")) == 1
    assert read_events(operator="documents-1d", lane="a")[0]["event"] == "z"


def test_read_events_skips_corrupt_lines() -> None:
    append_event("documents-0e", "a", "good")
    with events_path().open("a", encoding="utf-8") as handle:
        handle.write("not json\n\n")
    append_event("documents-0e", "a", "also-good")
    rows = read_events()
    assert [r["event"] for r in rows] == ["good", "also-good"]


def test_update_record_creates_then_mutates() -> None:
    created = update_record("documents-0e", "l", state=STATE_RUNNING, engine="cmd")
    assert created.state == STATE_RUNNING
    assert created.engine == "cmd"

    updated = update_record("documents-0e", "l", state=STATE_APPROVED, pr=99)
    assert updated.state == STATE_APPROVED
    assert updated.pr == 99
    # engine survives the partial update
    assert updated.engine == "cmd"


def test_update_record_can_clear_nullable_fields() -> None:
    update_record("documents-0e", "l", pid=111, pgid=111, pr=5)
    cleared = update_record("documents-0e", "l", pid=None, pgid=None)
    assert cleared.pid is None
    assert cleared.pgid is None
    assert cleared.pr == 5


def test_update_record_appends_an_event_when_asked() -> None:
    update_record("documents-0e", "l", state=STATE_RUNNING, event="lane.started")
    rows = read_events()
    assert [r["event"] for r in rows] == ["lane.started"]
    assert rows[0]["state"] == STATE_RUNNING


def test_update_record_without_event_writes_no_event() -> None:
    update_record("documents-0e", "l", state=STATE_RUNNING)
    assert read_events() == []


def test_tool_error_pct() -> None:
    record = LaneRecord(lane="l", operator="o", tool_calls=0, tool_errors=5)
    assert record.tool_error_pct == 0.0
    record = LaneRecord(lane="l", operator="o", tool_calls=4, tool_errors=1)
    assert record.tool_error_pct == 25.0


def test_age_and_idle_arithmetic() -> None:
    record = LaneRecord(lane="l", operator="o", started_ts=100.0, updated_ts=160.0)
    assert record.age_s(now=220.0) == 120.0
    assert record.idle_s(now=220.0) == 60.0


def test_corrupt_record_file_is_ignored() -> None:
    path = lane_state_path("documents-0e", "broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_record("documents-0e", "broken") is None
    assert all(r.lane != "broken" for r in iter_records())


def test_find_lane_refuses_ambiguity() -> None:
    save_record(LaneRecord(lane="same", operator="documents-0e"))
    save_record(LaneRecord(lane="same", operator="documents-1d"))
    assert find_lane("same") is None
    assert find_lane("same", operator="documents-0e") is not None


def test_paths_live_under_the_fleet_home() -> None:
    assert lanes_dir().name == "lanes"
    assert events_path().parent == lanes_dir()
    assert str(lane_state_path("op", "lane")).endswith("op/lane.json")


def test_process_starttime_of_self_is_readable() -> None:
    import os

    mine = process_starttime(os.getpid())
    assert mine is not None and mine > 0


def test_process_starttime_of_missing_pid_is_none() -> None:
    assert process_starttime(999_999_999) is None


def test_process_alive_for_self_and_for_garbage() -> None:
    import os

    assert process_alive(os.getpid()) is True
    assert process_alive(None) is False
    assert process_alive(0) is False
    assert process_alive(999_999_999) is False


def test_registry_module_exposes_state_constants() -> None:
    assert STATE_RUNNING in registry.KNOWN_STATES
    assert STATE_APPROVED in registry.TERMINAL_STATES
