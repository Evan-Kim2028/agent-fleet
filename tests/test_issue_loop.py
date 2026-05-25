"""Tests for issue comment trigger parsing and GitHub polling helpers."""

from unittest.mock import patch

from agent_fleet.issue_loop.github_ops import (
    as_comment_pages,
    flatten_issue_comment_pages,
    parse_paginated_json_arrays,
)
from agent_fleet.issue_loop.queue import _issue_is_open
from agent_fleet.issue_loop.triggers import (
    extract_issue_number,
    extract_persona,
    is_stop_command,
    is_watcher_comment,
)


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


def test_issue_is_open_uses_state_from_issue_view(tmp_path) -> None:
    with patch(
        "agent_fleet.issue_loop.queue.github_ops.issue_view",
        return_value={"state": "OPEN", "title": "t"},
    ):
        assert _issue_is_open(1940, cwd=tmp_path) is True
    with patch(
        "agent_fleet.issue_loop.queue.github_ops.issue_view",
        return_value={"state": "CLOSED", "title": "t"},
    ):
        assert _issue_is_open(1940, cwd=tmp_path) is False
