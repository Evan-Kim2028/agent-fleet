"""Terminal PR state (CLOSED/MERGED) skips park/fix — post-merge race guard."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from agent_fleet.pr_loop.github_ops import is_terminal_pr_state
from agent_fleet.pr_loop.lifecycle import LifecycleResult, park_for_human

if TYPE_CHECKING:
    from pathlib import Path


def test_is_terminal_pr_state_pure() -> None:
    assert is_terminal_pr_state("MERGED") is True
    assert is_terminal_pr_state("merged") is True
    assert is_terminal_pr_state(" CLOSED ") is True
    assert is_terminal_pr_state("OPEN") is False
    assert is_terminal_pr_state("") is False
    assert is_terminal_pr_state(None) is False  # type: ignore[arg-type]


def test_park_for_human_skips_when_pr_closed(tmp_path: Path) -> None:
    with (
        patch("agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed", return_value=True),
        patch("agent_fleet.pr_loop.lifecycle.github_ops.pr_comments") as comments,
        patch("agent_fleet.pr_loop.lifecycle.github_ops.post_pr_comment") as post,
    ):
        park_for_human(2504, "would park", repo_root=tmp_path)
    comments.assert_not_called()
    post.assert_not_called()


def test_lifecycle_body_skips_when_already_merged(tmp_path: Path) -> None:
    from agent_fleet.pr_loop.config import PrLoopConfig
    from agent_fleet.pr_loop.lifecycle import _run_pr_lifecycle_body
    from agent_fleet.repo import RepoConfig

    repo = RepoConfig(repo_root=tmp_path)
    loop = PrLoopConfig(enabled=True)
    with patch("agent_fleet.pr_loop.lifecycle.github_ops.is_pr_closed", return_value=True):
        result = _run_pr_lifecycle_body(
            pr_number=2504,
            branch="fleet/coder/2504-x",
            repo=repo,
            loop_config=loop,
            fleet_config=MagicMock(),
            worktree=tmp_path,
            skip_review_wait=True,
            persona="coder",
            fleet_log=MagicMock(),
        )
    assert isinstance(result, LifecycleResult)
    assert result.status == "merged"
    assert "closed" in result.detail.lower() or "merged" in result.detail.lower()
