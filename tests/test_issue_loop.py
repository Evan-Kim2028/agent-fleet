"""Tests for issue comment trigger parsing."""

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
