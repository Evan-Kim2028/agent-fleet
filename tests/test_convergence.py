"""Unit tests for orchestration convergence helpers."""

from __future__ import annotations

from agent_fleet.hooks import FleetTaskResult
from agent_fleet.orchestration.convergence import (
    FAILURE_STATUSES,
    PARTIAL_OK,
    SUCCESS_STATUSES,
    budget_upstream_context,
    compact_summary,
    roll_up_status,
)


def _result(
    *,
    goal: str = "task",
    status: str = "completed",
    error: str | None = None,
    summary: str | None = None,
    index: int = 0,
) -> FleetTaskResult:
    return FleetTaskResult(
        task_index=index,
        persona="coder",
        goal=goal,
        status=status,
        summary=summary,
        error=error,
        duration_seconds=1.0,
    )


def test_roll_up_status_all_success() -> None:
    results = [_result(status="completed"), _result(status="merged", index=1)]
    assert roll_up_status(results) == "success"


def test_roll_up_status_partial() -> None:
    results = [
        _result(status="completed"),
        _result(status="scope_violation", error="bad", index=1),
    ]
    assert roll_up_status(results) == "partial"


def test_roll_up_status_all_fail() -> None:
    results = [
        _result(status="error", error="e1"),
        _result(status="rejected", error="e2", index=1),
    ]
    assert roll_up_status(results) == "failure"


def test_compact_summary_all_success_bounded() -> None:
    results = [_result(goal=f"goal-{i}", summary="x" * 2000, index=i) for i in range(20)]
    summary = compact_summary(results, total_chars=400)
    assert len(summary) < 100
    assert "20/20" in summary


def test_compact_summary_includes_failures() -> None:
    results = [
        _result(status="completed"),
        _result(status="error", goal="failed task", error="boom", index=1),
    ]
    summary = compact_summary(results, total_chars=400)
    assert "failed task" in summary
    assert len(summary) <= 400


def test_budget_upstream_context_multi_dep_total_cap() -> None:
    outputs = {f"dep{i}": "y" * 2000 for i in range(4)}
    body = budget_upstream_context(outputs, tuple(outputs), total_budget=2000)
    assert len(body) <= 2000


def test_budget_upstream_context_single_dep_full_budget() -> None:
    outputs = {"only": "z" * 2000}
    body = budget_upstream_context(outputs, ("only",), total_budget=2000)
    assert len(body) >= 1900


def test_status_constants_exported() -> None:
    assert "completed" in SUCCESS_STATUSES
    assert "review_changes_requested" in PARTIAL_OK
    assert "error" in FAILURE_STATUSES
