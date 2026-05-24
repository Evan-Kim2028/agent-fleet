"""System memory probes for fleet admission and observability."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agent_fleet.observability.context import get_run_log

logger = logging.getLogger(__name__)


def _read_meminfo_kb(key: str) -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}:"):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def available_ram_gb() -> float | None:
    """Return MemAvailable from /proc/meminfo in GiB, or None if unreadable."""
    kb = _read_meminfo_kb("MemAvailable")
    if kb is None:
        return None
    return round(kb / (1024 * 1024), 2)


def process_rss_mb(pid: int) -> float | None:
    """Return resident set size for *pid* in MiB."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            kb = int(line.split()[1])
            return round(kb / 1024, 1)
    return None


def _is_playwright_mcp_cmdline(cmdline: str) -> bool:
    lowered = cmdline.lower()
    return "playwright" in lowered and ("mcp" in lowered or "@playwright/mcp" in lowered)


def iter_playwright_mcp_pids() -> list[int]:
    """Return PIDs for likely Playwright MCP server processes on this host."""
    pids: list[int] = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return pids
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        cmdline_path = entry / "cmdline"
        try:
            cmdline = cmdline_path.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="ignore")
        except OSError:
            continue
        if _is_playwright_mcp_cmdline(cmdline):
            pids.append(int(entry.name))
    return sorted(pids)


def count_playwright_mcp_processes() -> int:
    """Count likely Playwright MCP server processes on this host."""
    return len(iter_playwright_mcp_pids())


class PlaywrightSessionRegistry:
    """Track active fleet sessions that attach Playwright MCP."""

    _lock = threading.Lock()
    _active = 0

    @classmethod
    def register(cls) -> None:
        with cls._lock:
            cls._active += 1

    @classmethod
    def unregister(cls) -> int:
        with cls._lock:
            cls._active = max(0, cls._active - 1)
            return cls._active

    @classmethod
    def active_count(cls) -> int:
        with cls._lock:
            return cls._active


@dataclass(frozen=True)
class McpCleanupResult:
    before: int
    after: int
    waited_s: float
    force_killed: tuple[int, ...]
    cleaned: bool


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _terminate_pids(pids: list[int], *, grace_s: float = 2.0) -> tuple[int, ...]:
    """SIGTERM then SIGKILL any PIDs still alive after *grace_s*."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    if grace_s > 0:
        time.sleep(grace_s)
    killed: list[int] = []
    for pid in pids:
        if not _pid_alive(pid):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
        if not _pid_alive(pid):
            killed.append(pid)
    return tuple(killed)


def wait_for_playwright_mcp_cleanup(
    *,
    baseline: int | None = None,
    wait_s: float = 10.0,
    poll_interval_s: float = 0.5,
) -> McpCleanupResult:
    """Poll until Playwright MCP process count drops below *baseline*."""
    before = count_playwright_mcp_processes()
    target = baseline if baseline is not None else before
    t0 = time.monotonic()
    current = before
    while time.monotonic() - t0 < wait_s:
        current = count_playwright_mcp_processes()
        if current < target:
            break
        time.sleep(poll_interval_s)
    current = count_playwright_mcp_processes()
    waited_s = round(time.monotonic() - t0, 2)
    return McpCleanupResult(
        before=before,
        after=current,
        waited_s=waited_s,
        force_killed=(),
        cleaned=current < target,
    )


def cleanup_playwright_mcp_processes(
    *,
    baseline: int | None = None,
    wait_s: float = 10.0,
    poll_interval_s: float = 0.5,
    force_kill: bool = False,
) -> McpCleanupResult:
    """Wait for graceful Playwright MCP cleanup; optionally force-kill lingering PIDs."""
    result = wait_for_playwright_mcp_cleanup(
        baseline=baseline,
        wait_s=wait_s,
        poll_interval_s=poll_interval_s,
    )
    target = baseline if baseline is not None else result.before
    if result.cleaned or not force_kill or result.after == 0:
        return result
    if baseline is not None and result.after < baseline:
        return result

    pids = iter_playwright_mcp_pids()
    killed = _terminate_pids(pids)
    after = count_playwright_mcp_processes()
    return McpCleanupResult(
        before=result.before,
        after=after,
        waited_s=result.waited_s,
        force_killed=killed,
        cleaned=after < target,
    )


def memory_snapshot(*, label: str = "") -> dict[str, float | int | None]:
    """Collect a small memory snapshot for logging."""
    snap: dict[str, float | int | None] = {
        "available_ram_gb": available_ram_gb(),
        "playwright_mcp_processes": count_playwright_mcp_processes(),
    }
    prefix = f"{label} " if label else ""
    logger.info(
        "%smemory snapshot: available=%sGiB playwright_mcp=%s",
        prefix,
        snap["available_ram_gb"],
        snap["playwright_mcp_processes"],
    )
    run_log = get_run_log()
    if run_log is not None:
        run_log.memory(label=label or None, **snap)
    return snap
