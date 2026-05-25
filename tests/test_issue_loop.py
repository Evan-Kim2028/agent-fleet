"""Tests for issue comment trigger parsing and GitHub polling helpers."""

from typing import TYPE_CHECKING
from unittest.mock import patch

from agent_fleet.issue_loop.github_ops import (
    as_comment_pages,
    flatten_issue_comment_pages,
    parse_paginated_json_arrays,
)
from agent_fleet.issue_loop.triggers import (
    extract_issue_number,
    extract_persona,
    is_stop_command,
    is_watcher_comment,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_extract_persona() -> None:
    body = "Please handle this\n/agent --persona backend"
    assert extract_persona(body, r"/agent\s+--persona\s+(\S+)") == "backend"


def test_stop_command() -> None:
    assert is_stop_command("/agent stop", r"/agent\s+stop\b")


def test_watcher_marker() -> None:
    assert is_watcher_comment("hello <!-- agent-fleet-watcher -->", "<!-- agent-fleet-watcher -->")


def test_issue_number_from_url() -> None:
    assert extract_issue_number("https://api.github.com/repos/o/r/issues/1499") == 1499


def test_parse_slurped_comment_pages() -> None:
    stdout = '[[{"id": 1, "body": "/agent --persona backend"}]]'
    pages = parse_paginated_json_arrays(stdout)
    comments = flatten_issue_comment_pages(as_comment_pages(pages))
    assert len(comments) == 1
    assert comments[0]["body"] == "/agent --persona backend"


def test_parse_concatenated_comment_pages() -> None:
    stdout = '[{"id": 1}][{"id": 2}]'
    pages = parse_paginated_json_arrays(stdout)
    comments = flatten_issue_comment_pages(as_comment_pages(pages))
    assert [comment["id"] for comment in comments] == [1, 2]


def test_parse_single_comment_page() -> None:
    stdout = '[{"id": 1, "body": "hello"}, {"id": 2, "body": "/agent stop"}]'
    pages = parse_paginated_json_arrays(stdout)
    comments = flatten_issue_comment_pages(as_comment_pages(pages))
    assert len(comments) == 2


def test_parse_api_error_returns_empty_pages() -> None:
    stdout = '{"message":"Not Found","status":"404"}'
    pages = parse_paginated_json_arrays(stdout)
    assert pages == [{"message": "Not Found", "status": "404"}]


def test_issue_is_open_uses_state_from_issue_view(tmp_path: Path) -> None:
    with patch(
        "agent_fleet.issue_loop.queue.github_ops.issue_view",
        return_value={"state": "OPEN", "title": "t"},
    ):
        from agent_fleet.issue_loop.queue import _issue_is_open

        assert _issue_is_open(1940, cwd=tmp_path) is True
    with patch(
        "agent_fleet.issue_loop.queue.github_ops.issue_view",
        return_value={"state": "CLOSED", "title": "t"},
    ):
        from agent_fleet.issue_loop.queue import _issue_is_open

        assert _issue_is_open(1940, cwd=tmp_path) is False


def test_issue_numbers_in_branch() -> None:
    from agent_fleet.issue_loop.github_ops import issue_numbers_in_branch

    assert issue_numbers_in_branch("fleet/backend/1939-4408fc17") == {1939}
    assert issue_numbers_in_branch("fleet/frontend/#1933") == {1933}
    assert issue_numbers_in_branch("fleet/backend/#1939-extra") == {1939}


def test_run_issue_dispatch_skips_closed_issue(tmp_path: Path) -> None:
    """run_issue_dispatch returns 0 and does not invoke LocalFleetRunner when issue is closed."""
    from unittest.mock import MagicMock, patch

    from agent_fleet.issue_loop.config import IssueDispatchConfig
    from agent_fleet.issue_loop.dispatch import run_issue_dispatch
    from agent_fleet.repo import RepoConfig

    fake_repo = RepoConfig(repo_root=tmp_path, default_branch="main")

    with (
        patch("agent_fleet.issue_loop.dispatch.find_repo_config", return_value=fake_repo),
        patch(
            "agent_fleet.issue_loop.dispatch.github_ops.issue_view",
            return_value={"state": "CLOSED", "title": "x", "body": "y", "labels": [], "number": 42},
        ),
        patch(
            "agent_fleet.issue_loop.dispatch.github_ops.post_issue_comment",
        ) as mock_comment,
        patch(
            "agent_fleet.issue_loop.dispatch.github_ops.remove_label",
        ),
        patch(
            "agent_fleet.issue_loop.dispatch.github_ops.add_label",
        ),
        patch("agent_fleet.issue_loop.dispatch.load_fleet_config", return_value=MagicMock()),
        patch("agent_fleet.issue_loop.dispatch.LocalFleetRunner") as mock_runner,
    ):
        result = run_issue_dispatch(
            issue_number=42,
            comment_body="/agent --persona backend",
            repo_root=tmp_path,
            dispatch_config=IssueDispatchConfig(),
        )

    assert result == 0
    mock_runner.assert_not_called()
    # A skip comment should have been posted.
    assert mock_comment.call_count >= 1
    posted_body = mock_comment.call_args[0][1]
    assert "already closed" in posted_body
