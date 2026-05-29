"""Tests for watcher-level orphan Playwright MCP cleanup."""

from __future__ import annotations

from unittest.mock import patch

from agent_fleet.issue_loop.watcher import _cleanup_orphaned_playwright_mcp
from agent_fleet.memory import McpCleanupResult


def test_orphan_cleanup_skips_when_visual_audit_in_flight() -> None:
    state = {
        "in_flight": {
            "1824": [{"pid": 1, "persona": "frontend", "visual_audit": True}],
        }
    }
    with patch("agent_fleet.issue_loop.watcher.cleanup_playwright_mcp_processes") as cleanup:
        _cleanup_orphaned_playwright_mcp(state)
    cleanup.assert_not_called()


def test_orphan_cleanup_force_kills_when_idle() -> None:
    state: dict[str, object] = {"in_flight": {}}
    result = McpCleanupResult(
        before=3,
        after=0,
        waited_s=0.0,
        force_killed=(10, 11, 12),
        cleaned=True,
    )
    with (
        patch("agent_fleet.issue_loop.watcher.count_playwright_mcp_processes", return_value=3),
        patch(
            "agent_fleet.issue_loop.watcher.cleanup_playwright_mcp_processes",
            return_value=result,
        ) as cleanup,
    ):
        _cleanup_orphaned_playwright_mcp(state)
    cleanup.assert_called_once_with(force_kill=True)
