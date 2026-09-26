"""prodsafety-1: request_restart must not orphan a SIGTERM-ignoring component.

Same defect as correctness-1, framed as a production-safety leak: the watchdog's
no-progress rule calls ``request_restart`` repeatedly, and each call that lands
on a component which ignores SIGTERM leaks one live process, because
``stop_component`` neither waits nor escalates nor drops the Popen, and
``start`` overwrites ``self._procs[name]``.
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


def test_request_restart_does_not_duplicate_a_live_component() -> None:
    sup = Supervisor("op", _config(), clock=FakeClock())
    pids: list[int] = []
    try:
        assert sup.start("dispatcher") is True
        pids.append(sup._procs["dispatcher"].pid)
        assert _wait_until(
            lambda: (
                "armed"
                in component_log_path("op", "dispatcher").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
        ), "the child never installed its SIGTERM handler"

        assert sup.request_restart("dispatcher", reason="wedged") is True
        pids.append(sup._procs["dispatcher"].pid)
        time.sleep(0.4)

        live = [pid for pid in pids if pid_alive(pid)]
        assert len(live) <= 1, (
            f"{len(live)} live processes for the single 'dispatcher' role: {live}. "
            "The old child must be reaped and confirmed gone before a replacement "
            "is spawned."
        )
    finally:
        sup.shutdown()
        _reap_our_own(*pids)


def test_the_repeated_watchdog_path_does_not_leak_one_orphan_per_restart() -> None:
    sup = Supervisor("op", _config(), clock=FakeClock())
    pids: list[int] = []
    try:
        assert sup.start("dispatcher") is True
        pids.append(sup._procs["dispatcher"].pid)
        assert _wait_until(
            lambda: (
                "armed"
                in component_log_path("op", "dispatcher").read_text(
                    encoding="utf-8", errors="replace"
                )
            )
        )

        for _ in range(3):
            sup.request_restart("dispatcher", reason="wedged")
            pids.append(sup._procs["dispatcher"].pid)
            assert _wait_until(
                lambda: (
                    "armed"
                    in component_log_path("op", "dispatcher").read_text(
                        encoding="utf-8", errors="replace"
                    )
                )
            )

        time.sleep(0.4)
        live = [pid for pid in pids if pid_alive(pid)]
        assert len(live) <= 1, (
            f"three restarts left {len(live)} live components for one role: {live}. "
            "Each leaked process holds the worktree/gate slot it was spawned for."
        )
    finally:
        sup.shutdown()
        _reap_our_own(*pids)
