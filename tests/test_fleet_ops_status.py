"""The cross-operator lanes table, and the registry columns it reads."""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_fleet.fleet_ops import registry
from agent_fleet.fleet_ops.registry import (
    PHASE_GATE,
    PHASE_IMPL,
    STATE_APPROVED,
    STATE_RUNNING,
    STATE_STALLED,
    LaneRecord,
)
from agent_fleet.fleet_ops.status import (
    COLUMNS,
    derive_state,
    render_table,
    row_for,
    status_dicts,
    status_rows,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    """Point the lane registry at a tmp dir so tests never touch ~/.agent-fleet."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    return tmp_path


def _record(**overrides: Any) -> LaneRecord:  # noqa: ANN401
    base: dict[str, Any] = {"lane": "movers", "operator": "documents-0e", "state": STATE_RUNNING}
    return LaneRecord(**{**base, **overrides})


# ------------------------------------------------------------------- columns


def test_table_has_the_columns_documents_1d_required() -> None:
    headers = [h for h, _ in COLUMNS]
    for required in ("OPERATOR", "LANE", "REPO", "PR", "HEAD", "PHASE", "AGE", "LAST-STATUS"):
        assert required in headers, required


def test_row_values_line_up_with_the_headers() -> None:
    record = _record(
        repo="Evan-Kim2028/lake-of-rage",
        phase=PHASE_GATE,
        pr=3544,
        head="abcdef1234567890",
        status_line="12:00:00 PREMERGE-APPROVED abcdef123",
    )
    row = dict(zip([h for h, _ in COLUMNS], row_for(record, now=time.time()), strict=True))
    assert row["LANE"] == "movers"
    assert row["OPERATOR"] == "documents-0e"
    assert row["REPO"] == "Evan-Kim2028/lake-of-rage"
    assert row["PR"] == "#3544"
    assert row["HEAD"] == "abcdef123"
    assert row["PHASE"] == "gate"
    assert "PREMERGE-APPROVED" in row["LAST-STATUS"]


def test_missing_optional_fields_render_as_dashes() -> None:
    row = dict(zip([h for h, _ in COLUMNS], row_for(_record()), strict=True))
    assert row["REPO"] == "-"
    assert row["PR"] == "-"
    assert row["HEAD"] == "-"
    assert row["LAST-STATUS"] == "-"


# --------------------------------------------------------------------- state


def test_a_dead_process_is_reported_as_stalled_not_running() -> None:
    """A record left in `running` with a gone pid is how lanes used to get lost."""
    record = _record(pid=999_999_999)
    assert derive_state(record) == STATE_STALLED


def test_a_live_process_stays_running() -> None:
    import os

    record = _record(pid=os.getpid())
    assert derive_state(record) == STATE_RUNNING


def test_non_running_states_pass_through() -> None:
    assert derive_state(_record(state=STATE_APPROVED)) == STATE_APPROVED


# --------------------------------------------------------------------- table


def test_render_table_has_a_header_and_a_rule() -> None:
    text = render_table([_record(repo="o/r")], title="lanes (all operators)")
    lines = text.splitlines()
    assert lines[0] == "lanes (all operators)"
    assert "LANE" in lines[1] and "OPERATOR" in lines[1]
    assert set(lines[2]) <= {"-", " "}
    assert "movers" in lines[3]


def test_render_table_with_no_records_says_so() -> None:
    assert "(no lanes registered)" in render_table([])


def test_status_rows_span_operators_and_filter_by_one() -> None:
    registry.update_record("documents-0e", "alpha", state=STATE_RUNNING, phase=PHASE_IMPL)
    registry.update_record("documents-1d", "beta", state=STATE_APPROVED, phase=PHASE_GATE)

    all_rows = status_rows()
    assert {(r.operator, r.lane) for r in all_rows} == {
        ("documents-0e", "alpha"),
        ("documents-1d", "beta"),
    }
    only = status_rows(operator="documents-1d")
    assert [r.lane for r in only] == ["beta"]


def test_status_dicts_expose_the_json_contract() -> None:
    registry.update_record(
        "documents-1d",
        "beta",
        state=STATE_APPROVED,
        pr=42,
        head="abcdef123456",
        phase=PHASE_GATE,
        repo="Evan-Kim2028/silphcoanalytics",
        status_line="12:00:00 PREMERGE-APPROVED abcdef123",
    )
    row = status_dicts(operator="documents-1d")[0]
    assert row["pr"] == 42
    assert row["head_sha9"] == "abcdef123"
    assert row["phase"] == "gate"
    assert row["repo"] == "Evan-Kim2028/silphcoanalytics"
    assert "PREMERGE-APPROVED" in row["status_line"]


# ------------------------------------------------------------------ registry


def test_new_record_fields_survive_a_round_trip() -> None:
    record = _record(repo="o/r", phase=PHASE_GATE, status_line="12:00:00 X y")
    registry.save_record(record)
    loaded = registry.load_record("documents-0e", "movers")
    assert loaded is not None
    assert loaded.repo == "o/r"
    assert loaded.phase == PHASE_GATE
    assert loaded.status_line == "12:00:00 X y"


def test_loading_a_record_written_before_the_new_fields_defaults_phase() -> None:
    path = registry.lane_state_path("documents-0e", "legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"lane": "legacy", "operator": "documents-0e", "state": "idle"}', encoding="utf-8"
    )
    loaded = registry.load_record("documents-0e", "legacy")
    assert loaded is not None
    assert loaded.phase == PHASE_IMPL
    assert loaded.repo is None
