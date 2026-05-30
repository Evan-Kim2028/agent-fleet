"""Tests for agent_fleet.observability.run_store."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agent_fleet.observability.run_store import (
    append_run_index_row,
    fold_run_events,
    list_run_files,
    read_run_index,
    render_run_state,
    render_runs_table,
    resolve_run_path,
    run_index_path,
    run_is_terminal,
    run_log_total_tokens,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# (a) index round trip + merge
# ---------------------------------------------------------------------------


def test_index_round_trip_and_merge(tmp_path: Path) -> None:
    append_run_index_row(
        {"run_id": "r1", "goal": "G", "status": "running", "started_at": 1.0},
        runs_dir=tmp_path,
    )
    append_run_index_row(
        {"run_id": "r1", "status": "completed", "tokens": 1500},
        runs_dir=tmp_path,
    )
    rows = read_run_index(runs_dir=tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["run_id"] == "r1"
    assert r["goal"] == "G"
    assert r["status"] == "completed"
    assert r["tokens"] == 1500


# ---------------------------------------------------------------------------
# (b) read_run_index on empty/missing dir returns []
# ---------------------------------------------------------------------------


def test_read_run_index_missing_dir_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_dir"
    assert read_run_index(runs_dir=missing) == []


def test_read_run_index_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert read_run_index(runs_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# (c) newest-first ordering
# ---------------------------------------------------------------------------


def test_read_run_index_sorted_newest_first(tmp_path: Path) -> None:
    append_run_index_row(
        {"run_id": "older", "status": "completed", "started_at": 1.0},
        runs_dir=tmp_path,
    )
    append_run_index_row(
        {"run_id": "newer", "status": "completed", "started_at": 2.0},
        runs_dir=tmp_path,
    )
    rows = read_run_index(runs_dir=tmp_path)
    assert rows[0]["run_id"] == "newer"
    assert rows[1]["run_id"] == "older"


# ---------------------------------------------------------------------------
# (d) fold_run_events from constructed FleetEvent list
# ---------------------------------------------------------------------------


def _make_fleet_events() -> list[dict]:
    return [
        {"event": "run.start", "run_id": "r1", "ts": 1.0, "data": {"title": "my run"}},
        {"event": "program.phase", "run_id": "r1", "ts": 2.0, "data": {"title": "discover"}},
        {
            "event": "program.agent.start",
            "run_id": "r1",
            "ts": 3.0,
            "data": {"idx": 0, "persona": "backend", "goal": "scan"},
        },
        {
            "event": "program.agent.done",
            "run_id": "r1",
            "ts": 4.0,
            "data": {"idx": 0, "status": "completed", "tokens": 1500},
        },
        {"event": "run.end", "run_id": "r1", "ts": 5.0, "data": {"outcome": "completed"}},
    ]


def test_fold_run_events_state() -> None:
    state = fold_run_events(_make_fleet_events())
    assert state.status == "completed"
    assert "discover" in state.phases
    assert len(state.agents) == 1
    agent = state.agents[0]
    assert agent.persona == "backend"
    assert agent.observed_total_tokens == 1500


# ---------------------------------------------------------------------------
# (e) run_log_total_tokens: headline preferred, fallback sum
# ---------------------------------------------------------------------------


def test_run_log_total_tokens_headline_preferred() -> None:
    rows = [
        {
            "event": "efficiency.headline",
            "data": {"total_tokens": 9000},
        },
        {
            "event": "program.agent.done",
            "data": {"idx": 0, "status": "completed", "tokens": 100},
        },
    ]
    assert run_log_total_tokens(rows) == 9000


def test_run_log_total_tokens_fallback_sum() -> None:
    rows = [
        {
            "event": "program.agent.done",
            "data": {"idx": 0, "status": "completed", "tokens": 100},
        },
        {
            "event": "program.agent.done",
            "data": {"idx": 1, "status": "completed", "tokens": 200},
        },
    ]
    assert run_log_total_tokens(rows) == 300


# ---------------------------------------------------------------------------
# (f) resolve_run_path: exact, prefix, latest, non-matching
# ---------------------------------------------------------------------------


def test_resolve_run_path_exact_stem(tmp_path: Path) -> None:
    (tmp_path / "aaa111.jsonl").touch()
    (tmp_path / "bbb222.jsonl").touch()
    (tmp_path / "index.jsonl").touch()
    result = resolve_run_path("aaa111", runs_dir=tmp_path)
    assert result is not None
    assert result.stem == "aaa111"


def test_resolve_run_path_prefix(tmp_path: Path) -> None:
    (tmp_path / "aaa111.jsonl").touch()
    (tmp_path / "bbb222.jsonl").touch()
    (tmp_path / "index.jsonl").touch()
    result = resolve_run_path("bbb", runs_dir=tmp_path)
    assert result is not None
    assert result.stem == "bbb222"


def test_resolve_run_path_latest(tmp_path: Path) -> None:
    a = tmp_path / "aaa111.jsonl"
    b = tmp_path / "bbb222.jsonl"
    a.touch()
    time.sleep(0.01)
    b.touch()
    (tmp_path / "index.jsonl").touch()
    result = resolve_run_path("latest", runs_dir=tmp_path)
    assert result is not None
    assert result.stem == "bbb222"


def test_resolve_run_path_empty_string_latest(tmp_path: Path) -> None:
    a = tmp_path / "aaa111.jsonl"
    b = tmp_path / "bbb222.jsonl"
    a.touch()
    time.sleep(0.01)
    b.touch()
    result = resolve_run_path("", runs_dir=tmp_path)
    assert result is not None
    assert result.stem == "bbb222"


def test_resolve_run_path_no_match_returns_none(tmp_path: Path) -> None:
    (tmp_path / "aaa111.jsonl").touch()
    result = resolve_run_path("zzz999", runs_dir=tmp_path)
    assert result is None


def test_list_run_files_excludes_index(tmp_path: Path) -> None:
    (tmp_path / "aaa111.jsonl").touch()
    (tmp_path / "bbb222.jsonl").touch()
    (tmp_path / "index.jsonl").touch()
    files = list_run_files(tmp_path)
    names = {p.name for p in files}
    assert "index.jsonl" not in names
    assert "aaa111.jsonl" in names
    assert "bbb222.jsonl" in names


# ---------------------------------------------------------------------------
# (g) renderers are pure
# ---------------------------------------------------------------------------


def test_render_runs_table_with_rows() -> None:
    rows: list[dict[str, object]] = [
        {
            "run_id": "r1",
            "status": "completed",
            "tokens": 5,
            "started_at": 1.0,
            "goal": "do x",
        }
    ]
    out = render_runs_table(rows)
    assert "RUN ID" in out
    assert "r1" in out
    assert "do x" in out


def test_render_runs_table_empty_returns_no_runs_string() -> None:
    out = render_runs_table([])
    assert out  # non-empty
    assert "No runs" in out or "no runs" in out.lower()


def test_render_run_state_contains_expected_fields() -> None:
    state = fold_run_events(_make_fleet_events())
    out = render_run_state(state, tokens=1500)
    assert "backend" in out
    assert "1500" in out
    assert "discover" in out


def test_run_is_terminal_for_terminal_states() -> None:
    from agent_fleet.orchestration.journal import RunEvent, RunEventKind, fold_journal

    events = [
        RunEvent(run_id="r1", seq=0, kind=RunEventKind.run_started, ts=1.0),
        RunEvent(
            run_id="r1",
            seq=1,
            kind=RunEventKind.run_completed,
            ts=2.0,
            payload={"status": "completed"},
        ),
    ]
    state = fold_journal(events)
    assert run_is_terminal(state) is True


def test_run_is_terminal_false_for_running() -> None:
    from agent_fleet.orchestration.journal import RunEvent, RunEventKind, fold_journal

    events = [
        RunEvent(run_id="r1", seq=0, kind=RunEventKind.run_started, ts=1.0),
    ]
    state = fold_journal(events)
    assert run_is_terminal(state) is False


def test_run_index_path_uses_runs_dir(tmp_path: Path) -> None:
    p = run_index_path(runs_dir=tmp_path)
    assert p.parent == tmp_path
    assert p.name == "index.jsonl"
