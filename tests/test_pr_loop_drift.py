"""Tests for Phase 4 drift detection in pr_loop lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_fleet.pr_loop.config import PrLoopConfig
from agent_fleet.pr_loop.github_ops import MergeTreeResult
from agent_fleet.pr_loop.lifecycle import (
    _DRIFT_ISSUE_MARKER,
    _DRIFT_PR_MARKER,
    LifecycleResult,
    _detect_drift,
    _issue_number_from_branch,
    _marker_within_window,
)

# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


def test_issue_number_from_branch_fleet_pattern() -> None:
    assert _issue_number_from_branch("fleet/coder/1399-abc12345") == 1399


def test_issue_number_from_branch_agent_fleet_pattern() -> None:
    assert _issue_number_from_branch("agent_fleet/data/1532-xyz") == 1532


def test_issue_number_from_branch_no_match() -> None:
    assert _issue_number_from_branch("main") is None
    assert _issue_number_from_branch("feature/add-stuff") is None


def test_marker_within_window_present_recent() -> None:
    now = datetime.now(tz=UTC)
    recent = (now - timedelta(hours=1)).isoformat()
    comments = [{"body": f"body {_DRIFT_PR_MARKER}", "createdAt": recent}]
    assert _marker_within_window(comments, _DRIFT_PR_MARKER) is True


def test_marker_within_window_present_old() -> None:
    now = datetime.now(tz=UTC)
    old = (now - timedelta(hours=25)).isoformat()
    comments = [{"body": f"body {_DRIFT_PR_MARKER}", "createdAt": old}]
    assert _marker_within_window(comments, _DRIFT_PR_MARKER) is False


def test_marker_within_window_absent() -> None:
    comments = [{"body": "no marker here", "createdAt": datetime.now(tz=UTC).isoformat()}]
    assert _marker_within_window(comments, _DRIFT_PR_MARKER) is False


def test_marker_within_window_no_timestamp_treated_as_recent() -> None:
    comments = [{"body": f"body {_DRIFT_PR_MARKER}"}]
    assert _marker_within_window(comments, _DRIFT_PR_MARKER) is True


# ---------------------------------------------------------------------------
# _detect_drift integration tests (subprocess-mocked)
# ---------------------------------------------------------------------------


def _loop_config(*, drift_check: bool = True) -> PrLoopConfig:
    return PrLoopConfig(enabled=True, drift_check=drift_check)


def _call_detect_drift(
    tmp_path: Path,
    branch: str = "fleet/coder/42-abc",
    loop_config: PrLoopConfig | None = None,
) -> LifecycleResult | None:
    return _detect_drift(
        pr_number=42,
        branch=branch,
        worktree=tmp_path,
        repo_root=tmp_path,
        loop_config=loop_config or _loop_config(),
    )


def _make_proc(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# Case 1: no drift — merge-base says main is already ancestor
def test_detect_drift_no_drift(tmp_path: Path) -> None:
    fetch_ok = _make_proc(0)
    ancestor_ok = _make_proc(0)  # is-ancestor returns 0 → no drift

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [fetch_ok, ancestor_ok]
        result = _call_detect_drift(tmp_path)

    assert result is None
    assert mock_run.call_count == 2


# Case 2: auto-mergeable drift — merge-base says NOT ancestor, merge-tree is clean
def test_detect_drift_auto_mergeable(tmp_path: Path) -> None:
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)  # main NOT ancestor → drift exists
    clean_tree = MergeTreeResult(clean=True, conflict_files=())

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=clean_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.update_branch",
        ) as mock_update,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail]
        result = _call_detect_drift(tmp_path)

    assert result is not None
    assert result.status == "behind"
    mock_update.assert_called_once_with(42, cwd=tmp_path)


# Case 3: unresolvable drift, first call → PR comment + close + issue reopen
def test_detect_drift_conflict_first_call(tmp_path: Path) -> None:
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    sha_proc = _make_proc(0, stdout="abc1234\n")
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("pipeline/model.py",))

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=[],  # no prior drift comment
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.close_pr") as mock_close,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue",
        ) as mock_reopen,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.issue_comments",
            return_value=[],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment",
        ) as mock_issue_comment,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail, sha_proc]
        result = _call_detect_drift(tmp_path, branch="fleet/coder/42-abc")

    assert result is not None
    assert result.status == "drift"

    # PR comment posted with drift marker
    mock_post_pr.assert_called_once()
    pr_body = mock_post_pr.call_args[0][0]
    assert _DRIFT_PR_MARKER in pr_body
    assert "pipeline/model.py" in pr_body
    assert "abc1234" in pr_body

    # PR closed exactly once
    mock_close.assert_called_once_with(42, cwd=tmp_path)

    # Issue reopened and replan comment posted
    mock_reopen.assert_called_once_with(42, cwd=tmp_path)
    mock_issue_comment.assert_called_once()
    issue_body = mock_issue_comment.call_args[0][0]
    assert _DRIFT_ISSUE_MARKER in issue_body
    assert "pipeline/model.py" in issue_body


# Case 4: idempotency — drift comment already within 24h → all actions skipped
def test_detect_drift_conflict_idempotent(tmp_path: Path) -> None:
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    recent_ts = datetime.now(tz=UTC).isoformat()
    existing_drift_comment = [{"body": f"old comment {_DRIFT_PR_MARKER}", "createdAt": recent_ts}]
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("src/a.py",))

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=existing_drift_comment,
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.close_pr") as mock_close,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue") as mock_reopen,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment") as mock_issue_comment,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail]
        result = _call_detect_drift(tmp_path)

    assert result is not None
    assert result.status == "drift"
    # All actions must be skipped because the marker is already present
    mock_post_pr.assert_not_called()
    mock_close.assert_not_called()
    mock_reopen.assert_not_called()
    mock_issue_comment.assert_not_called()


# Case 5: drift_check=False → entire detection skipped
def test_detect_drift_disabled(tmp_path: Path) -> None:
    with patch("subprocess.run") as mock_run:
        result = _call_detect_drift(tmp_path, loop_config=_loop_config(drift_check=False))

    assert result is None
    mock_run.assert_not_called()


# Case 6: branch without parseable issue number → comment+close PR but skip issue reopen
def test_detect_drift_no_issue_in_branch(tmp_path: Path) -> None:
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    sha_proc = _make_proc(0, stdout="deadbeef\n")
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("Makefile",))

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=[],
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.close_pr") as mock_close,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue") as mock_reopen,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment") as mock_issue_comment,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail, sha_proc]
        result = _call_detect_drift(tmp_path, branch="feature/no-issue-number")

    assert result is not None
    assert result.status == "drift"
    # PR actions still happen
    mock_post_pr.assert_called_once()
    mock_close.assert_called_once()
    # Issue actions skipped because no issue number parseable
    mock_reopen.assert_not_called()
    mock_issue_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_pr_loop_config_drift_check_default() -> None:
    from agent_fleet.pr_loop.config import load_pr_loop_config

    cfg = load_pr_loop_config(Path("/tmp"), {"pr_loop": {"enabled": True}})
    assert cfg is not None
    assert cfg.drift_check is True


def test_pr_loop_config_drift_check_disabled() -> None:
    from agent_fleet.pr_loop.config import load_pr_loop_config

    cfg = load_pr_loop_config(Path("/tmp"), {"pr_loop": {"enabled": True, "drift_check": False}})
    assert cfg is not None
    assert cfg.drift_check is False


# ---------------------------------------------------------------------------
# MergeTreeResult dataclass
# ---------------------------------------------------------------------------


def test_merge_tree_result_clean() -> None:
    r = MergeTreeResult(clean=True, conflict_files=())
    assert r.clean is True
    assert r.conflict_files == ()


def test_merge_tree_result_conflict() -> None:
    r = MergeTreeResult(clean=False, conflict_files=("a.py", "b.py"))
    assert r.clean is False
    assert "a.py" in r.conflict_files
