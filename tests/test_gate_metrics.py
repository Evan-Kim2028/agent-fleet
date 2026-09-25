"""Tests for gate metrics — the record that makes convergence observable."""

from __future__ import annotations

import json
import re
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import cast

from agent_fleet.gate.metrics import (
    OUTCOME_CONVERGED,
    OUTCOME_STALLED,
    GateMetrics,
    RoundMetric,
    metrics_path,
    read_metrics,
    render_metrics_table,
    summarize_rows,
)

# ---------------------------------------------------------------------------
# RoundMetric — the convergence rule lives here
# ---------------------------------------------------------------------------


def test_round_progresses_only_when_it_shrank_and_broke_nothing() -> None:
    """Progress = strictly fewer failing AND no new failures. Both, not either."""
    assert RoundMetric(1, "abc", 5, fixed=2, new_failures=0).progressed
    assert not RoundMetric(1, "abc", 5, fixed=2, new_failures=1).progressed
    assert not RoundMetric(1, "abc", 5, fixed=0, new_failures=0).progressed


def test_round_without_deltas_has_not_progressed() -> None:
    """Round 0 is the measurement baseline, not a fix attempt."""
    assert not RoundMetric(0, "abc", 5).progressed


# ---------------------------------------------------------------------------
# GateMetrics
# ---------------------------------------------------------------------------


def test_metrics_stamp_a_time_when_missing() -> None:
    metric = GateMetrics(run_id="g1", repo="r", pr=1, start_sha="a" * 40)
    assert metric.at


def test_metrics_keep_an_explicit_time() -> None:
    metric = GateMetrics(run_id="g1", repo="r", pr=1, start_sha="a", at="2026-01-01T00:00:00")
    assert metric.at == "2026-01-01T00:00:00"


def test_failing_by_round_tracks_the_trace() -> None:
    metric = GateMetrics(
        run_id="g1",
        repo="r",
        pr=3,
        start_sha="a",
        rounds=[
            RoundMetric(0, "aaa", 5),
            RoundMetric(1, "bbb", 3, fixed=2),
            RoundMetric(2, "ccc", 1, fixed=2),
        ],
    )
    assert metric.failing_by_round == [5, 3, 1]
    assert metric.round_count == 3


def test_to_dict_is_json_serialisable() -> None:
    metric = GateMetrics(
        run_id="g1",
        repo="r",
        pr=3,
        start_sha="a",
        head_sha="b",
        outcome=OUTCOME_CONVERGED,
        candidates=9,
        confirmed=4,
        rejected=5,
        untestable=1,
        untestable_real=1,
        rounds=[RoundMetric(0, "aaa", 2), RoundMetric(1, "bbb", 0, fixed=2)],
        reasons=["nope"],
    )
    payload = metric.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["outcome"] == OUTCOME_CONVERGED
    assert payload["failing_by_round"] == [2, 0]
    assert payload["candidates"] == 9
    rounds = cast("list[dict[str, object]]", payload["rounds"])
    assert rounds[1]["fixed"] == 2


# ---------------------------------------------------------------------------
# append / read
# ---------------------------------------------------------------------------


def test_append_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    for pr in (1, 2, 3):
        GateMetrics(run_id=f"g{pr}", repo="r", pr=pr, start_sha="a").append_metrics(path)
    rows = read_metrics(path)
    assert [row["pr"] for row in rows] == [1, 2, 3]


def test_append_creates_the_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "deep" / "nested" / "metrics.jsonl"
    GateMetrics(run_id="g", repo="r", pr=1, start_sha="a").append_metrics(path)
    assert path.is_file()


def test_append_never_raises_on_an_unwritable_path(tmp_path: Path) -> None:
    """A metrics write failure must never fail a run that already has a verdict."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("x", encoding="utf-8")
    GateMetrics(run_id="g", repo="r", pr=1, start_sha="a").append_metrics(blocked / "metrics.jsonl")


def test_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_metrics(tmp_path / "nope.jsonl") == []


def test_read_tolerates_a_partial_final_line(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"pr": 1}\n{"pr": 2}\n{"pr": 3, "partia', encoding="utf-8")
    assert [row["pr"] for row in read_metrics(path)] == [1, 2]


def test_read_skips_non_object_lines(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"pr": 1}\n[1,2,3]\n"text"\n', encoding="utf-8")
    assert len(read_metrics(path)) == 1


def test_read_limit_takes_the_newest(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    for pr in range(1, 6):
        GateMetrics(run_id=f"g{pr}", repo="r", pr=pr, start_sha="a").append_metrics(path)
    rows = read_metrics(path, limit=2)
    assert [row["pr"] for row in rows] == [4, 5]


def test_metrics_path_honours_the_fleet_home(tmp_path: Path) -> None:
    import os

    os.environ["AGENT_FLEET_HOME"] = str(tmp_path)
    try:
        assert metrics_path() == tmp_path / "gate" / "metrics.jsonl"
    finally:
        del os.environ["AGENT_FLEET_HOME"]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_table_on_no_rows() -> None:
    assert render_metrics_table([]) == "No gate runs recorded yet."


def test_table_shows_the_funnel_and_the_trace() -> None:
    rows: list[dict[str, object]] = [
        {
            "at": "2026-09-25T10:00:00",
            "pr": 42,
            "candidates": 9,
            "confirmed": 3,
            "rejected": 6,
            "untestable": 1,
            "failing_by_round": [5, 3, 0],
            "outcome": OUTCOME_CONVERGED,
        }
    ]
    text = render_metrics_table(rows)
    assert "42" in text
    assert "9" in text and "3" in text and "6" in text
    assert "5,3,0" in text
    assert OUTCOME_CONVERGED in text


def test_table_tolerates_garbage_fields() -> None:
    """On-disk rows are untrusted; a bad field must not crash the command."""
    text = render_metrics_table(
        [{"pr": None, "candidates": "x", "failing_by_round": "nope", "outcome": None}]
    )
    # Bad counts render as 0 and a bad round list as the "-" placeholder.
    assert re.search(r"\b0\s+0\s+0\s+0\s+0\b", text)
    assert "-" in text
    assert "None" in text


def test_table_shows_a_dash_when_no_rounds() -> None:
    text = render_metrics_table([{"pr": 1, "failing_by_round": [], "outcome": "stalled"}])
    assert "-" in text


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


def test_summary_of_no_runs() -> None:
    assert summarize_rows([]) == {
        "runs": 0,
        "outcomes": {},
        "rounds_total": 0,
        "candidates_total": 0,
        "confirmed_total": 0,
        "approval_rate": 0.0,
    }


def test_summary_counts_outcomes_and_approval_rate() -> None:
    rows: list[dict[str, object]] = [
        {"outcome": OUTCOME_CONVERGED, "failing_by_round": [3, 0], "candidates": 4, "confirmed": 2},
        {"outcome": OUTCOME_STALLED, "failing_by_round": [3, 3], "candidates": 6, "confirmed": 1},
        {"outcome": OUTCOME_CONVERGED, "failing_by_round": [1, 0], "candidates": 2, "confirmed": 1},
    ]
    summary = summarize_rows(rows)
    assert summary["runs"] == 3
    assert summary["outcomes"] == {OUTCOME_CONVERGED: 2, OUTCOME_STALLED: 1}
    assert summary["rounds_total"] == 6
    assert summary["candidates_total"] == 12
    assert summary["confirmed_total"] == 4
    assert summary["approval_rate"] == 0.667
