"""Tests for memory probes."""

from __future__ import annotations

from unittest.mock import patch

from agent_fleet.memory import (
    McpCleanupResult,
    PlaywrightSessionRegistry,
    available_ram_gb,
    cleanup_playwright_mcp_processes,
    memory_snapshot,
    wait_for_playwright_mcp_cleanup,
)


def test_available_ram_gb_positive() -> None:
    value = available_ram_gb()
    assert value is None or value > 0


def test_memory_snapshot_keys() -> None:
    snap = memory_snapshot(label="test")
    assert "available_ram_gb" in snap
    assert "playwright_mcp_processes" in snap
    assert isinstance(snap["playwright_mcp_processes"], int)


def test_playwright_session_registry_tracks_active_sessions() -> None:
    PlaywrightSessionRegistry._active = 0  # noqa: SLF001
    PlaywrightSessionRegistry.register()
    PlaywrightSessionRegistry.register()
    assert PlaywrightSessionRegistry.active_count() == 2
    assert PlaywrightSessionRegistry.unregister() == 1
    assert PlaywrightSessionRegistry.unregister() == 0
    assert PlaywrightSessionRegistry.unregister() == 0


def test_wait_for_playwright_mcp_cleanup_detects_drop() -> None:
    counts = iter([3, 2, 1])

    def _count() -> int:
        return next(counts, 1)

    with patch("agent_fleet.memory.count_playwright_mcp_processes", side_effect=_count):
        with patch("agent_fleet.memory.time.sleep"):
            result = wait_for_playwright_mcp_cleanup(baseline=3, wait_s=1.0, poll_interval_s=0.1)

    assert result.before == 3
    assert result.after == 1
    assert result.cleaned is True


def test_cleanup_playwright_mcp_processes_force_kills_when_stuck() -> None:
    wait_result = McpCleanupResult(
        before=2,
        after=2,
        waited_s=10.0,
        force_killed=(),
        cleaned=False,
    )
    with patch("agent_fleet.memory.wait_for_playwright_mcp_cleanup", return_value=wait_result):
        with patch("agent_fleet.memory.iter_playwright_mcp_pids", return_value=[111, 222]):
            with patch("agent_fleet.memory._terminate_pids", return_value=(111, 222)) as kill:
                with patch("agent_fleet.memory.count_playwright_mcp_processes", return_value=0):
                    result = cleanup_playwright_mcp_processes(
                        baseline=2,
                        force_kill=True,
                    )

    kill.assert_called_once_with([111, 222])
    assert result.force_killed == (111, 222)
    assert result.after == 0
    assert result.cleaned is True


def test_cleanup_orphan_force_kills_remaining_after_partial_graceful_exit() -> None:
    wait_result = McpCleanupResult(
        before=2,
        after=1,
        waited_s=10.0,
        force_killed=(),
        cleaned=True,
    )
    with patch("agent_fleet.memory.wait_for_playwright_mcp_cleanup", return_value=wait_result):
        with patch("agent_fleet.memory.iter_playwright_mcp_pids", return_value=[999]):
            with patch("agent_fleet.memory._terminate_pids", return_value=(999,)) as kill:
                with patch("agent_fleet.memory.count_playwright_mcp_processes", return_value=0):
                    result = cleanup_playwright_mcp_processes(force_kill=True)

    kill.assert_called_once_with([999])
    assert result.after == 0
