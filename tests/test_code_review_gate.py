"""Tests for review-run gating on small green diffs (U4)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path


def _make_task() -> MagicMock:
    task = MagicMock()
    task.persona = "coder"
    task.allowed_paths = ()
    return task


def _make_persona() -> MagicMock:
    persona = MagicMock()
    persona.name = "coder"
    persona.allowed_paths = ()
    return persona


def _make_resolver(persona: MagicMock) -> MagicMock:
    resolver = MagicMock()
    resolver.load.return_value = persona
    return resolver


def _green_scope_result() -> dict[str, object]:
    return {"phase": "scope", "passed": True, "exit_code": 0, "stdout": "", "stderr": ""}


def _green_verify_result() -> dict[str, object]:
    return {"phase": "verify", "passed": True, "exit_code": 0, "stdout": "", "stderr": ""}


def _failing_verify_result() -> dict[str, object]:
    return {
        "phase": "verify",
        "passed": False,
        "exit_code": 1,
        "stdout": "",
        "stderr": "tests failed",
    }


def test_review_skipped_when_green_gates_and_small_diff(tmp_path: Path) -> None:
    """When gates are green and changed lines < threshold, review is skipped."""
    from agent_fleet.code_review.loop import _REVIEW_SKIP_LINES_THRESHOLD, _rerun_quality_gates

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_green_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=_REVIEW_SKIP_LINES_THRESHOLD - 1),
        patch(f"{_loop}.run_structured_review_phase") as mock_review,
    ):
        results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_review.assert_not_called()
    assert exit_code == 0
    review_results = [r for r in results if r.get("phase") == "review"]
    assert len(review_results) == 1
    r = review_results[0]
    assert r["skipped"] is True
    assert r["verdict"] == "approve"
    assert r["passed"] is True
    assert r["exit_code"] == 0
    assert str(_REVIEW_SKIP_LINES_THRESHOLD) in r["reason"]


def test_review_runs_when_diff_at_threshold(tmp_path: Path) -> None:
    """When changed lines == threshold, review is NOT skipped."""
    from agent_fleet.code_review.loop import _REVIEW_SKIP_LINES_THRESHOLD, _rerun_quality_gates

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    review_result = {
        "phase": "review",
        "verdict": "approve",
        "skipped": False,
        "passed": True,
        "exit_code": 0,
        "summary": "looks good",
        "stdout": "",
        "stderr": "",
    }

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_green_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=_REVIEW_SKIP_LINES_THRESHOLD),
        patch(f"{_loop}.run_structured_review_phase", return_value=review_result) as mock_review,
    ):
        results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_review.assert_called_once()
    assert exit_code == 0
    review_results = [r for r in results if r.get("phase") == "review"]
    assert len(review_results) == 1
    assert review_results[0].get("skipped") is False


def test_review_runs_when_diff_above_threshold(tmp_path: Path) -> None:
    """When changed lines > threshold, review runs normally."""
    from agent_fleet.code_review.loop import _REVIEW_SKIP_LINES_THRESHOLD, _rerun_quality_gates

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    review_result = {
        "phase": "review",
        "verdict": "approve",
        "skipped": False,
        "passed": True,
        "exit_code": 0,
        "summary": "looks good",
        "stdout": "",
        "stderr": "",
    }

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_green_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=_REVIEW_SKIP_LINES_THRESHOLD + 100),
        patch(f"{_loop}.run_structured_review_phase", return_value=review_result) as mock_review,
    ):
        _results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_review.assert_called_once()
    assert exit_code == 0


def test_review_runs_when_verify_failed(tmp_path: Path) -> None:
    """When verify fails, gate never fires — review path is not reached."""
    from agent_fleet.code_review.loop import _rerun_quality_gates

    persona = _make_persona()
    resolver = _make_resolver(persona)
    task = _make_task()

    _loop = "agent_fleet.code_review.loop"
    with (
        patch(f"{_loop}.collect_changed_files", return_value=["src/a.py"]),
        patch(f"{_loop}.run_scope_phase", return_value=_green_scope_result()),
        patch(f"{_loop}.run_verify_phases", return_value=[_failing_verify_result()]),
        patch(f"{_loop}.changed_lines", return_value=5) as mock_cl,
        patch(f"{_loop}.run_structured_review_phase") as mock_review,
    ):
        results, _summary, exit_code, _files = _rerun_quality_gates(
            backend=MagicMock(),
            resolver=resolver,
            task=task,
            workspace=tmp_path,
            timeout_s=60,
            repo=None,
            implementation_summary="done",
        )

    mock_cl.assert_not_called()
    mock_review.assert_not_called()
    assert exit_code == 1
    review_results = [r for r in results if r.get("phase") == "review"]
    assert len(review_results) == 0


def test_skip_threshold_default_value() -> None:
    """The threshold constant must be 50."""
    from agent_fleet.code_review.loop import _REVIEW_SKIP_LINES_THRESHOLD

    assert _REVIEW_SKIP_LINES_THRESHOLD == 50
