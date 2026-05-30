"""Tests for agent_fleet.emit — the emit seam.

Covers:
- status→exit-code table for all documented statuses
- comment-markdown branch (fmt="comment")
- dataclass serialization via default=str
- dict serialization
- list-of-dataclasses serialization
- FleetRunResult.outcome (full pipeline) ok-set
- FleetTaskResult.status (dispatcher pipeline) ok-set — documented distinction preserved
"""

from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_fleet.emit import emit

# ---------------------------------------------------------------------------
# Minimal stubs so we don't need real backend imports
# ---------------------------------------------------------------------------


@dataclass
class _TaskResult:
    status: str
    summary: str | None = None
    error: str | None = None

    @property
    def __dict__(self) -> dict[str, object]:  # type: ignore[override]
        return {"status": self.status, "summary": self.summary, "error": self.error}


@dataclass
class _RunResult:
    outcome: str
    run_id: str = "r1"
    error: str | None = None

    @property
    def __dict__(self) -> dict[str, object]:  # type: ignore[override]
        return {"outcome": self.outcome, "run_id": self.run_id, "error": self.error}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_captured(result: object, fmt: str = "json") -> tuple[int, str]:
    """Call emit() and capture stdout."""
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        code = emit(result, fmt=fmt)
    finally:
        sys.stdout = old_stdout
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# Exit-code table for FleetTaskResult.status (dispatcher pipeline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, expected_code",
    [
        # ok statuses — exit 0
        ("completed", 0),
        ("merged", 0),
        ("decompose_partial", 0),
        ("dag_partial", 0),
        # not-ok statuses — exit 1
        ("error", 1),
        ("rejected", 1),
        ("scope_violation", 1),
        ("decompose_failed", 1),
        ("complexity_underestimated", 1),
    ],
)
def test_task_status_exit_code(status: str, expected_code: int) -> None:
    """FleetTaskResult.status → correct exit code from the table."""
    # emit() takes a list of task results (as cmd_run does for dispatcher path)
    result = [_TaskResult(status=status).__dict__]
    code, _ = _emit_captured(result)
    assert code == expected_code, f"status={status!r}: expected {expected_code}, got {code}"


# ---------------------------------------------------------------------------
# Exit-code table for FleetRunResult.outcome (full pipeline)
# Documented distinction: full pipeline uses .outcome, not .status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, expected_code",
    [
        # ok outcomes — exit 0
        ("completed", 0),
        ("completed_noop", 0),
        ("review_changes_requested", 0),
        ("decompose_partial", 0),
        ("dag_partial", 0),
        # not-ok outcomes — exit 1
        ("error", 1),
        ("rejected", 1),
        ("scope_violation", 1),
    ],
)
def test_run_outcome_exit_code(outcome: str, expected_code: int) -> None:
    """FleetRunResult.outcome → correct exit code from the table.

    The full pipeline uses .outcome; the dispatcher pipeline uses .status.
    These are different fields on different result types — emit() must not
    conflate them.  Full-pipeline ok-set includes review_changes_requested
    and completed_noop which are NOT in the dispatcher ok-set.
    """
    result = _RunResult(outcome=outcome).__dict__
    code, _ = _emit_captured(result)
    assert code == expected_code, f"outcome={outcome!r}: expected {expected_code}, got {code}"


# ---------------------------------------------------------------------------
# review verdict exit-code table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict, expected_code",
    [
        ("approve", 0),
        ("block", 1),
        ("request_changes", 1),
    ],
)
def test_review_verdict_exit_code(verdict: str, expected_code: int) -> None:
    """Review verdict dict → correct exit code."""
    result = {"verdict": verdict, "summary": "x", "issues": []}
    code, _ = _emit_captured(result)
    assert code == expected_code, f"verdict={verdict!r}: expected {expected_code}, got {code}"


# ---------------------------------------------------------------------------
# comment-markdown branch
# ---------------------------------------------------------------------------


def test_emit_comment_format_prints_markdown() -> None:
    """fmt='comment' must print result['comment_markdown'], not full JSON."""
    result = {
        "verdict": "approve",
        "comment_markdown": "## Review\nLGTM",
        "issues": [],
    }
    code, out = _emit_captured(result, fmt="comment")
    assert code == 0
    assert "## Review" in out
    assert "LGTM" in out
    # Must NOT be a JSON dump of the whole dict
    assert out.strip() == "## Review\nLGTM"


def test_emit_comment_format_respects_verdict_exit_code() -> None:
    """fmt='comment' with verdict=block → exit 1."""
    result = {
        "verdict": "block",
        "comment_markdown": "## Block\nDo not merge.",
        "issues": [{"detail": "bad"}],
    }
    code, out = _emit_captured(result, fmt="comment")
    assert code == 1
    assert "Block" in out


# ---------------------------------------------------------------------------
# dict without known status/verdict/outcome — default exit 0
# ---------------------------------------------------------------------------


def test_emit_plain_dict_default_exit_0() -> None:
    """A dict with no status/verdict/outcome keys defaults to exit 0."""
    result = {"personas": ["coder"], "pipelines": ["simple"]}
    code, out = _emit_captured(result)
    assert code == 0
    data = json.loads(out)
    assert data["personas"] == ["coder"]


# ---------------------------------------------------------------------------
# dict with 'error' key → exit 1
# ---------------------------------------------------------------------------


def test_emit_dict_with_error_key_exits_1() -> None:
    """A dict with an 'error' key exits 1."""
    result = {"error": "something went wrong", "data": None}
    code, _ = _emit_captured(result)
    assert code == 1


# ---------------------------------------------------------------------------
# dataclass serialization via default=str
# ---------------------------------------------------------------------------


def test_emit_dataclass_serializes_via_default_str() -> None:
    """Dataclass with Path fields serializes without TypeError (default=str)."""

    @dataclass
    class _WithPath:
        name: str
        path: Path

        @property
        def __dict__(self) -> dict[str, object]:  # type: ignore[override]
            return {"name": self.name, "path": self.path}

    result = _WithPath(name="test", path=Path("/some/path")).__dict__
    code, out = _emit_captured(result)
    assert code == 0
    data = json.loads(out)
    assert data["path"] == "/some/path"


# ---------------------------------------------------------------------------
# list of dicts — first element drives exit code
# ---------------------------------------------------------------------------


def test_emit_list_first_element_drives_exit_code() -> None:
    """For a list result, the first element's status drives the exit code."""
    results = [{"status": "error"}, {"status": "completed"}]
    code, _ = _emit_captured(results)
    assert code == 1


def test_emit_empty_list_exits_1() -> None:
    """An empty list result exits 1 (no success confirmed)."""
    code, _ = _emit_captured([])
    assert code == 1


# ---------------------------------------------------------------------------
# Full-pipeline vs dispatcher ok-set: documented distinction
# The test below asserts review_changes_requested is ok for full pipeline
# but NOT for dispatcher (it's not in the dispatcher ok-set).
# This is the critical distinction the plan says must NOT be flattened.
# ---------------------------------------------------------------------------


def test_review_changes_requested_is_ok_for_full_pipeline() -> None:
    """review_changes_requested in FleetRunResult.outcome → exit 0 (full pipeline ok)."""
    result = {"outcome": "review_changes_requested"}
    code, _ = _emit_captured(result)
    assert code == 0


def test_review_changes_requested_is_not_ok_for_dispatcher() -> None:
    """review_changes_requested is NOT in the dispatcher ok-set.

    The dispatcher path produces FleetTaskResult.status, which never includes
    review_changes_requested.  If it somehow appeared as status, it must not
    be treated as ok (it's not in the dispatcher table).
    """
    result = [{"status": "review_changes_requested"}]
    code, _ = _emit_captured(result)
    # review_changes_requested is not a known-ok dispatcher status → exit 1
    assert code == 1


def test_completed_noop_is_ok_for_full_pipeline_only() -> None:
    """completed_noop is an ok outcome for the full pipeline, not the dispatcher."""
    # Full pipeline: outcome=completed_noop → ok
    full_result = {"outcome": "completed_noop"}
    full_code, _ = _emit_captured(full_result)
    assert full_code == 0

    # Dispatcher: status=completed_noop → not in ok-set → exit 1
    dispatcher_result = [{"status": "completed_noop"}]
    dispatch_code, _ = _emit_captured(dispatcher_result)
    assert dispatch_code == 1
