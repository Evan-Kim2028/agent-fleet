"""Tests for BacklogDispatcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from agent_fleet.capacity import FleetCapacity
from agent_fleet.capacity.config import PerIssueLimits
from agent_fleet.issue_loop.backlog_dispatcher import (
    BACKLOG_MARKER,
    BacklogDispatcher,
    DispatchTickResult,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
_FRESH_TS = (_NOW - timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
_STALE_TS = (_NOW - timedelta(seconds=400)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_repo(tmp_path: Path) -> MagicMock:
    repo = MagicMock()
    repo.repo_root = tmp_path
    repo.display_name = "test/repo"
    return repo


def _make_dispatcher(
    tmp_path: Path,
    *,
    max_dispatches: int = 4,
    marker_freshness_s: int = 300,
    default_persona: str = "data",
) -> BacklogDispatcher:
    repo = _make_repo(tmp_path)
    capacity = FleetCapacity(
        max_dispatches=max_dispatches,
        per_issue=PerIssueLimits(default=3, visual_audit=1),
    )
    sp = tmp_path / ".agent-fleet-state.json"
    return BacklogDispatcher(
        repo,
        capacity,
        sp,
        label="fleet-ready",
        persona_label_prefix="fleet-persona/",
        default_persona=default_persona,
        marker_freshness_s=marker_freshness_s,
    )


def _issue(
    number: int,
    labels: list[str] | None = None,
    comments: list[dict] | None = None,  # type: ignore[type-arg]
) -> dict:  # type: ignore[type-arg]
    raw_labels = [{"name": lbl} for lbl in (labels or [])]
    return {
        "number": number,
        "labels": raw_labels,
        "comments": comments or [],
    }


# ---------------------------------------------------------------------------
# autouse: prevent real `pid_is_dispatch` from being called on fake PIDs
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_pid() -> Generator[None]:
    with patch("agent_fleet.in_flight.pid_is_dispatch", return_value=True):
        yield


# ---------------------------------------------------------------------------
# Test: two eligible issues are dispatched
# ---------------------------------------------------------------------------


def test_two_eligible_issues_dispatched(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    issues = [_issue(100), _issue(101)]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    assert result.considered == 2
    assert len(result.dispatched) == 2
    assert result.dispatched[0] == (100, "data")
    assert result.dispatched[1] == (101, "data")
    assert not result.skipped_for_reason
    assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# Test: issue in in_flight is skipped
# ---------------------------------------------------------------------------


def test_in_flight_issue_skipped(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    sp = tmp_path / ".agent-fleet-state.json"
    sp.write_text(
        json.dumps(
            {"in_flight": {"100": [{"pid": 9999, "persona": "data", "visual_audit": False}]}}
        ),
        encoding="utf-8",
    )
    issues = [_issue(100), _issue(101)]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    assert result.considered == 2
    assert result.skipped_for_reason.get("in_flight") == 1
    assert len(result.dispatched) == 1
    assert result.dispatched[0] == (101, "data")
    assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Test: issue with agent-running/<n> mutex label is skipped
# ---------------------------------------------------------------------------


def test_mutex_label_issue_skipped(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    issues = [_issue(100, labels=["fleet-ready", "agent-running/100"]), _issue(101)]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    assert result.considered == 2
    assert result.skipped_for_reason.get("mutex_label") == 1
    assert len(result.dispatched) == 1
    assert result.dispatched[0] == (101, "data")
    assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Test: issue with recent backlog-dispatcher marker is skipped
# ---------------------------------------------------------------------------


def test_recent_marker_issue_skipped(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, marker_freshness_s=300)
    fresh_comment = {
        "body": f"/agent --persona data {BACKLOG_MARKER}",
        "createdAt": _FRESH_TS,
    }
    issues = [_issue(100, comments=[fresh_comment]), _issue(101)]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    assert result.considered == 2
    assert result.skipped_for_reason.get("recent_marker") == 1
    assert len(result.dispatched) == 1
    assert result.dispatched[0] == (101, "data")
    assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Test: stale marker does NOT skip
# ---------------------------------------------------------------------------


def test_stale_marker_not_skipped(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, marker_freshness_s=300)
    stale_comment = {
        "body": f"/agent --persona data {BACKLOG_MARKER}",
        "createdAt": _STALE_TS,
    }
    issues = [_issue(100, comments=[stale_comment])]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    assert len(result.dispatched) == 1
    assert result.dispatched[0] == (100, "data")
    assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Test: persona label picks that persona; without it falls back to default
# ---------------------------------------------------------------------------


def test_persona_label_picked(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, default_persona="data")
    issues = [
        _issue(100, labels=["fleet-ready", "fleet-persona/backend"]),
        _issue(101, labels=["fleet-ready"]),
    ]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    assert result.dispatched[0] == (100, "backend")
    assert result.dispatched[1] == (101, "data")
    assert mock_post.call_count == 2


# ---------------------------------------------------------------------------
# Test: capacity gate refusing stops the iteration
# ---------------------------------------------------------------------------


def test_capacity_gate_stops_iteration(tmp_path: Path) -> None:
    # max_dispatches=0 means any try_admit will refuse
    dispatcher = _make_dispatcher(tmp_path, max_dispatches=0)
    issues = [_issue(100), _issue(101), _issue(102)]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    # The first issue hits capacity; iteration stops.
    assert len(result.dispatched) == 0
    assert sum(result.skipped_for_reason.values()) >= 1
    # At most one issue is counted as skipped (iteration stopped early)
    assert result.considered == 1
    assert mock_post.call_count == 0


# ---------------------------------------------------------------------------
# Test: idempotent — second call within freshness window skips
# ---------------------------------------------------------------------------


def test_idempotent_second_call_skips(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path, marker_freshness_s=300)
    issues = [_issue(100)]
    posted_comments: list[dict] = []  # type: ignore[type-arg]

    def fake_post(_issue_number: int, persona: str) -> None:
        posted_comments.append(
            {
                "body": f"/agent --persona {persona} {BACKLOG_MARKER}",
                "createdAt": _FRESH_TS,
            }
        )

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment", side_effect=fake_post),
    ):
        result1 = dispatcher.dispatch_once(_NOW)

    # Second call: pretend the issue now has the comment we just posted.
    issues_with_marker = [_issue(100, comments=posted_comments)]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues_with_marker),
        patch.object(dispatcher, "_issue_has_open_pr", return_value=False),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post2,
    ):
        result2 = dispatcher.dispatch_once(_NOW)

    assert len(result1.dispatched) == 1
    assert len(result2.dispatched) == 0
    assert result2.skipped_for_reason.get("recent_marker") == 1
    assert mock_post2.call_count == 0


# ---------------------------------------------------------------------------
# Test: open PR causes skip
# ---------------------------------------------------------------------------


def test_open_pr_skips_issue(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)
    issues = [_issue(100), _issue(101)]

    with (
        patch.object(dispatcher, "_list_label_issues", return_value=issues),
        patch(
            "agent_fleet.issue_loop.backlog_dispatcher.github_ops.open_fleet_pr_issue_numbers",
            return_value={100},
        ),
        patch.object(dispatcher, "_post_dispatch_comment") as mock_post,
    ):
        result = dispatcher.dispatch_once(_NOW)

    assert result.skipped_for_reason.get("open_pr") == 1
    assert len(result.dispatched) == 1
    assert result.dispatched[0] == (101, "data")
    assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# Test: DispatchTickResult dataclass defaults are correct
# ---------------------------------------------------------------------------


def test_dispatch_tick_result_defaults() -> None:
    r = DispatchTickResult()
    assert r.considered == 0
    assert r.skipped_for_reason == {}
    assert r.dispatched == []


# ---------------------------------------------------------------------------
# Test: marker comment body format
# ---------------------------------------------------------------------------


def test_post_dispatch_comment_format(tmp_path: Path) -> None:
    dispatcher = _make_dispatcher(tmp_path)

    with patch(
        "agent_fleet.issue_loop.backlog_dispatcher.github_ops.post_issue_comment"
    ) as mock_comment:
        dispatcher._post_dispatch_comment(42, "frontend")

    mock_comment.assert_called_once()
    issue_arg, body_arg = mock_comment.call_args[0][:2]
    assert issue_arg == 42
    assert "/agent --persona frontend" in body_arg
    assert BACKLOG_MARKER in body_arg
