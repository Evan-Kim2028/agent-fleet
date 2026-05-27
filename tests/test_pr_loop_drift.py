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
    default_branch: str = "main",
) -> LifecycleResult | None:
    return _detect_drift(
        pr_number=42,
        branch=branch,
        worktree=tmp_path,
        repo_root=tmp_path,
        loop_config=loop_config or _loop_config(),
        default_branch=default_branch,
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


# Case 3: unresolvable drift, first call → close PR, reopen issue, PR comment last
def test_detect_drift_conflict_first_call(tmp_path: Path) -> None:
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    sha_proc = _make_proc(0, stdout="abc1234\n")
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("pipeline/model.py",))

    call_order: list[str] = []

    def record_close(*_a: object, **_kw: object) -> bool:
        call_order.append("close")
        return True

    def record_reopen(*_a: object, **_kw: object) -> bool:
        call_order.append("reopen")
        return True

    def record_post_pr(*_a: object, **_kw: object) -> None:
        call_order.append("post_pr")

    def record_post_issue(*_a: object, **_kw: object) -> None:
        call_order.append("post_issue")

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed",
            return_value=False,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.close_pr",
            side_effect=record_close,
        ) as mock_close,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue",
            side_effect=record_reopen,
        ) as mock_reopen,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.issue_comments",
            return_value=[],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=[],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment",
            side_effect=record_post_issue,
        ) as mock_issue_comment,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment",
            side_effect=record_post_pr,
        ) as mock_post_pr,
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

    # close and reopen must happen BEFORE post_pr (marker is last)
    assert call_order.index("close") < call_order.index("post_pr")
    assert call_order.index("reopen") < call_order.index("post_pr")
    assert call_order.index("post_issue") < call_order.index("post_pr")


# Case 4: idempotency — PR closed AND source issue already has replan marker → all
# destructive actions skipped. Closing the PR alone is not enough; a prior cycle
# that closed the PR but failed to reopen+replan must keep retrying, which means
# the gate also requires _DRIFT_ISSUE_MARKER present on the source issue.
def test_detect_drift_conflict_idempotent(tmp_path: Path) -> None:
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("src/a.py",))
    recent = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    pr_comments_with_marker = [{"body": f"drift body {_DRIFT_PR_MARKER}", "createdAt": recent}]
    issue_comments_with_marker = [
        {"body": f"replan body {_DRIFT_ISSUE_MARKER}", "createdAt": recent}
    ]

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed",
            return_value=True,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=pr_comments_with_marker,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.issue_comments",
            return_value=issue_comments_with_marker,
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
    # All destructive actions skipped: PR already closed AND both markers present
    mock_post_pr.assert_not_called()
    mock_close.assert_not_called()
    mock_reopen.assert_not_called()
    mock_issue_comment.assert_not_called()


def test_detect_drift_pr_marker_but_no_issue_marker_retries(tmp_path: Path) -> None:
    """PR closed + PR marker but issue replan marker missing → retry replan path."""
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("src/a.py",))
    recent = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    pr_comments_with_marker = [{"body": f"drift body {_DRIFT_PR_MARKER}", "createdAt": recent}]

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed",
            return_value=True,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=pr_comments_with_marker,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.issue_comments",
            return_value=[],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.close_pr",
            return_value=True,
        ) as mock_close,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue",
            return_value=True,
        ) as mock_reopen,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment") as mock_issue_comment,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail, _make_proc(0, stdout="sha\n")]
        result = _call_detect_drift(tmp_path, branch="fleet/coder/42-abc")

    assert result is not None
    assert result.status == "drift"
    # post_issue_comment uses check=False so silent failure can leave issue
    # un-replanned; the retry must re-run reopen + replan even though PR marker is present.
    mock_close.assert_called_once()
    mock_reopen.assert_called_once_with(42, cwd=tmp_path)
    mock_issue_comment.assert_called_once()
    # PR marker should not be re-posted since _marker_within_window finds the recent one.
    mock_post_pr.assert_not_called()


# Case 5: drift_check=False → entire detection skipped
def test_detect_drift_disabled(tmp_path: Path) -> None:
    with patch("subprocess.run") as mock_run:
        result = _call_detect_drift(tmp_path, loop_config=_loop_config(drift_check=False))

    assert result is None
    mock_run.assert_not_called()


# Case 6: branch without parseable issue number → close PR + PR comment but skip issue reopen
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
            "agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed",
            return_value=False,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.close_pr",
            return_value=True,
        ) as mock_close,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue") as mock_reopen,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment") as mock_issue_comment,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=[],
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
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
# Fix 1: drift uses PR head ref (origin/<branch>), not local HEAD
# ---------------------------------------------------------------------------


def test_detect_drift_uses_pr_head_ref_not_local_head(tmp_path: Path) -> None:
    """merge_tree_against must be called with origin/<branch>, not 'HEAD'."""
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    clean_tree = MergeTreeResult(clean=True, conflict_files=())

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=clean_tree,
        ) as mock_merge_tree,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.update_branch"),
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail]
        _call_detect_drift(tmp_path, branch="fleet/coder/42-feature")

    merge_tree_call = mock_merge_tree.call_args
    head_arg = merge_tree_call[0][1]
    assert head_arg == "origin/fleet/coder/42-feature", (
        f"Expected origin/<branch> but got {head_arg!r}"
    )
    assert head_arg != "HEAD"


def test_detect_drift_fetch_includes_pr_branch(tmp_path: Path) -> None:
    """git fetch must include both default_branch and the PR branch."""
    fetch_ok = _make_proc(0)
    ancestor_ok = _make_proc(0)

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [fetch_ok, ancestor_ok]
        _call_detect_drift(tmp_path, branch="fleet/coder/42-feature")

    fetch_call_args = mock_run.call_args_list[0][0][0]
    assert "main" in fetch_call_args
    assert "fleet/coder/42-feature" in fetch_call_args


# ---------------------------------------------------------------------------
# Fix 2: close+reopen partial failure returns retry-safe result (no marker)
# ---------------------------------------------------------------------------


def test_detect_drift_close_failure_no_marker_posted(tmp_path: Path) -> None:
    """If close_pr fails, the drift marker must NOT be posted (allow retry)."""
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("a.py",))

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed",
            return_value=False,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.close_pr",
            return_value=False,
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue") as mock_reopen,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail, _make_proc(0, stdout="sha1234\n")]
        result = _call_detect_drift(tmp_path)

    assert result is not None
    assert result.status == "drift"
    mock_post_pr.assert_not_called()
    mock_reopen.assert_not_called()


def test_detect_drift_reopen_failure_no_pr_marker_posted(tmp_path: Path) -> None:
    """If reopen_issue fails, the PR drift marker must NOT be posted (allow retry).

    A retry that finds an old marker on the PR would treat the work as done and
    silently drop the replan, so we must abort BEFORE the PR comment when the
    reopen step itself failed.
    """
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    sha_proc = _make_proc(0, stdout="sha9999\n")
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("svc/x.py",))

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed",
            return_value=False,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.close_pr",
            return_value=True,
        ) as mock_close,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue",
            return_value=False,
        ) as mock_reopen,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.issue_comments",
            return_value=[],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=[],
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment") as mock_issue_comment,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail, sha_proc]
        result = _call_detect_drift(tmp_path, branch="fleet/coder/42-abc")

    assert result is not None
    assert result.status == "drift"
    assert "reopen failed" in result.detail
    mock_close.assert_called_once()
    mock_reopen.assert_called_once()
    # PR marker NOT posted so next cycle retries
    mock_post_pr.assert_not_called()
    mock_issue_comment.assert_not_called()


def test_detect_drift_pr_closed_but_issue_not_replanned_retries(tmp_path: Path) -> None:
    """PR already closed but no replan marker on issue → re-run reopen + replan.

    A prior cycle that closed the PR but crashed before reopening the issue
    must not be silently abandoned. The gate skips destructive actions only
    when BOTH the PR is closed AND the source issue has a recent replan marker.
    """
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    conflict_tree = MergeTreeResult(clean=False, conflict_files=("x.py",))

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=conflict_tree,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed",
            return_value=True,
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.close_pr",
            return_value=True,
        ) as mock_close,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.reopen_issue",
            return_value=True,
        ) as mock_reopen,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.issue_comments",
            return_value=[],
        ),
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.pr_comments",
            return_value=[],
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_issue_comment") as mock_issue_comment,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail, _make_proc(0, stdout="sha\n")]
        result = _call_detect_drift(tmp_path, branch="fleet/coder/42-abc")

    assert result is not None
    assert result.status == "drift"
    # close_pr is idempotent (already-closed = success); reopen + replan + marker run
    mock_close.assert_called_once()
    mock_reopen.assert_called_once_with(42, cwd=tmp_path)
    mock_issue_comment.assert_called_once()
    mock_post_pr.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 4: non-main default_branch is used instead of hardcoded "origin/main"
# ---------------------------------------------------------------------------


def test_detect_drift_non_main_default_branch(tmp_path: Path) -> None:
    """Drift check must use the configured default_branch, not 'main'."""
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    clean_tree = MergeTreeResult(clean=True, conflict_files=())

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=clean_tree,
        ) as mock_merge_tree,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.update_branch"),
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail]
        result = _call_detect_drift(tmp_path, default_branch="develop")

    assert result is not None
    assert result.status == "behind"
    assert "develop" in result.detail

    base_arg = mock_merge_tree.call_args[0][0]
    assert base_arg == "origin/develop"

    fetch_call_args = mock_run.call_args_list[0][0][0]
    assert "develop" in fetch_call_args
    assert "main" not in fetch_call_args


# ---------------------------------------------------------------------------
# Fix 5: merge-tree exit code > 1 is a git error, NOT a conflict
# ---------------------------------------------------------------------------


def test_merge_tree_result_git_error_flag() -> None:
    r = MergeTreeResult(clean=False, conflict_files=(), git_error=True)
    assert r.git_error is True
    assert r.clean is False


def test_detect_drift_merge_tree_git_error_skips_close(tmp_path: Path) -> None:
    """When merge-tree returns a git error (exit > 1), drift is skipped entirely."""
    fetch_ok = _make_proc(0)
    ancestor_fail = _make_proc(1)
    git_error_tree = MergeTreeResult(clean=False, conflict_files=(), git_error=True)

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "agent_fleet.pr_loop.lifecycle.github_ops.merge_tree_against",
            return_value=git_error_tree,
        ),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.close_pr") as mock_close,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as mock_post_pr,
    ):
        mock_run.side_effect = [fetch_ok, ancestor_fail]
        result = _call_detect_drift(tmp_path)

    assert result is None
    mock_close.assert_not_called()
    mock_post_pr.assert_not_called()


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
    assert r.git_error is False


def test_merge_tree_result_conflict() -> None:
    r = MergeTreeResult(clean=False, conflict_files=("a.py", "b.py"))
    assert r.clean is False
    assert "a.py" in r.conflict_files
    assert r.git_error is False
