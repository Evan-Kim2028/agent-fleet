"""j_1: ``shutdown()`` must escalate a stubborn component to a group KILL.

``ServeConfig.shutdown_grace_s`` is documented as the number of seconds the
supervisor waits "before escalating to a group KILL", and
:func:`agent_fleet.serve.procs.escalate_kill_group` exists to be that
escalation. The shutdown path only sends the TERM and then bounds the wait by
the same grace, so a component that ignores or blocks SIGTERM keeps running
forever after serve is gone — holding its worktree and its gate slot, with
nothing left to signal it.

This is deliberately about a *hostile* child. A well-behaved one exits on TERM
and the missing escalation never shows up; the property under test is that the
supervisor does not depend on a child's cooperation to stop it.

Real processes throughout, because "did the group actually get killed" is
exactly the thing a mock runner cannot answer.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.supervisor import Supervisor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: A child that traps SIGTERM and never exits. The only way to stop it is a KILL.
#: ``READY`` is printed *after* the handler is installed, so a test that waits
#: for it can never race the interpreter's start-up and observe a child that
#: merely happens to be slow to install its handler.
STUBBORN = (
    f"{sys.executable} -c "
    "'import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    'print("READY", flush=True); time.sleep(300)\''
)

#: How long to wait for the child to install its SIGTERM handler.
READY_TIMEOUT_S = 10.0

#: How long to wait for a killed process to disappear from ``/proc``.
EXIT_TIMEOUT_S = 5.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


@pytest.fixture
def reaper() -> Iterator[list[int]]:
    """Collect pids to SIGKILL on teardown.

    The cleanup is the point: when the assertion below fails the process really
    is still running, and leaving a SIGTERM-proof ``time.sleep(300)`` behind on a
    shared box is its own kind of mess. Only ever signals pids the test itself
    spawned, by exact pid.
    """
    pids: list[int] = []
    try:
        yield pids
    finally:
        for pid in pids:
            if pid > 0 and pid_alive(pid):
                os.kill(pid, 9)


def _wait_gone(pid: int, *, timeout: float = EXIT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.02)
    return not pid_alive(pid)


def _read_log(path: Path) -> str:
    # The log is appended to by a live child; a read that races the create or
    # the append is not evidence of anything, so it just means "not ready yet".
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _wait_for_handler(pid: int, *, timeout: float = READY_TIMEOUT_S) -> bool:
    """Block until the child has provably installed its SIGTERM handler."""
    log = component_log_path("op", "dispatcher")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_alive(pid) and "READY" in _read_log(log):
            return True
        time.sleep(0.02)
    return False


def _config(command: str, *, shutdown_grace_s: float) -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=cmd,
            backoff_initial_s=1.0,
            backoff_max_s=60.0,
            crash_threshold=3,
            crash_window_minutes=15,
            no_progress_restarts=2,
            no_progress_window_minutes=30,
            shutdown_grace_s=shutdown_grace_s,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=shutdown_grace_s,
        components={
            "dispatcher": spec("dispatcher", command),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def test_shutdown_escalates_to_kill_for_a_component_that_ignores_sigterm(
    reaper: list[int],
) -> None:
    """A TERM-proof component must not survive shutdown()."""
    grace = 1.0
    sup = Supervisor("op", _config(STUBBORN, shutdown_grace_s=grace))

    assert sup.start("dispatcher") is True
    pid = sup.children["dispatcher"].pid
    assert pid is not None
    reaper.append(pid)

    assert _wait_for_handler(pid), (
        f"pid {pid} never reached the point where its SIGTERM handler is installed; "
        "the test cannot exercise TERM-vs-KILL without that"
    )

    started = time.monotonic()
    sup.shutdown()
    elapsed = time.monotonic() - started

    # It must be gone, not merely TERMed and forgotten. A KILL cannot be caught,
    # blocked or ignored, so "not alive any more" is the whole contract.
    assert _wait_gone(pid), (
        f"shutdown() returned after {elapsed:.2f}s and left pid {pid} running: "
        "it TERMed the group but never escalated to the group KILL that "
        "shutdown_grace_s documents, so a SIGTERM-proof component outlives serve"
    )
