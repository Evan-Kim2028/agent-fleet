"""correctness-1: request_restart must not leave two live processes for one role.

``request_restart`` (supervisor.py:562) calls ``stop_component`` and then
``start`` immediately. ``stop_component`` only sends SIGTERM via
``terminate_group`` -- it never waits, never escalates to KILL, and never
removes the entry from ``self._procs``. ``start`` then overwrites
``self._procs[name]``, so the original Popen is dropped and the still-running
original process is never reaped, never re-terminated, and never known to
``shutdown()``. The module's stated invariant is "Re-attach, never double-start".
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.supervisor import Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: A child that ignores SIGTERM and keeps running -- the "wedged" case
#: request_restart exists for. It prints "armed" once the handler is installed.
WEDGED = (
    f"{sys.executable} -c 'import signal,time\n"
    f"signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    f'print("armed", flush=True)\n'
    f"time.sleep(300)'"
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _config() -> ServeConfig:
    def spec(name: str, command: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=command,
            backoff_initial_s=0.0,
            backoff_max_s=0.0,
            crash_threshold=3,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=0.3,
        components={
            "dispatcher": spec("dispatcher", WEDGED),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _reap_our_own(*pids: int) -> None:
    """Kill only the exact pids this test spawned (process-safety rule)."""
    for pid in {p for p in pids if p and p > 0}:
        with suppress(OSError):
            os.kill(pid, 9)


def test_request_restart_never_leaves_the_original_running() -> None:
    sup = Supervisor("op", _config(), clock=FakeClock())
    pid_a: int | None = None
    pid_b: int | None = None
    try:
        assert sup.start("dispatcher") is True
        proc_a = sup._procs["dispatcher"]
        pid_a = proc_a.pid
        assert _wait_until(
            lambda: (
                "armed"
                in component_log_path("op", "dispatcher").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
        ), "the child never installed its SIGTERM handler"

        assert sup.request_restart("dispatcher", reason="no progress") is True
        pid_b = sup._procs["dispatcher"].pid
        assert pid_b != pid_a, "precondition: a replacement was spawned"

        # Give the TERM a fair chance to land before judging.
        time.sleep(0.4)
        assert not pid_alive(pid_a), (
            f"the original child {pid_a} survived the restart and is now a second "
            f"live process for the 'dispatcher' role (replacement is {pid_b}). "
            "stop_component() must wait and escalate, not TERM and move on."
        )
        assert len(sup._procs) == 1
    finally:
        # Clean up first, then judge, so a failing assertion never leaks a child.
        sup.shutdown()
        time.sleep(0.3)
        survived_shutdown = pid_alive(pid_a)
        _reap_our_own(pid_a, pid_b)
        assert not survived_shutdown, (
            f"child {pid_a} was orphaned: shutdown() iterates self._procs, which no "
            "longer contains it, so the original is never terminated"
        )


def test_shutdown_reaches_every_process_it_spawned() -> None:
    """A restart must not leak a process that shutdown() can no longer see."""
    sup = Supervisor("op", _config(), clock=FakeClock())
    spawned: list[int] = []
    try:
        assert sup.start("dispatcher") is True
        spawned.append(sup._procs["dispatcher"].pid)
        assert _wait_until(
            lambda: (
                "armed"
                in component_log_path("op", "dispatcher").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
        )
        sup.request_restart("dispatcher", reason="no progress")
        spawned.append(sup._procs["dispatcher"].pid)

        sup.shutdown()
        time.sleep(0.4)
        alive = [pid for pid in spawned if pid_alive(pid)]
        assert not alive, f"these processes outlived shutdown(): {alive}"
    finally:
        sup.shutdown()
        _reap_our_own(*spawned)
