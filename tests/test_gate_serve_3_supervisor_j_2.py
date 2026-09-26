"""j_2: a component this supervisor *adopted* must still be stopped at shutdown.

The module's two headline properties are "re-attach to a component that is
already running" and "stop every child on shutdown". As written they cannot
both hold for the same component: :meth:`Supervisor.adopt` records the pid and
fingerprint in ``ChildState`` but never registers a ``Popen``, and
:meth:`Supervisor.shutdown` only walks ``self._procs``. So the restart-safe
path — the whole point of the pid file and the fingerprint — re-attaches to a
process and then abandons it.

The consequence is a process nobody owns: it keeps running after serve is
gone, holding its worktree and its gate slot, and the *next* supervisor will
adopt it again and again abandon it. Only the two headline behaviours are under
test here; the deeper truth-versus-cache problem that follows is not.

Real processes throughout: the property is about a live process surviving two
successive supervisor objects, which is not something a mock can express.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.paths import component_pid_path
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.supervisor import Supervisor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: An ordinary, well-behaved component: it exits promptly on SIGTERM. The claim
#: is not about a stubborn child, it is that a *cooperative* one is not signalled
#: at all, because it was never in ``_procs`` to be iterated.
SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"

#: How long to wait for a TERMed child to disappear from ``/proc``.
EXIT_TIMEOUT_S = 5.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


@pytest.fixture
def reaper() -> Iterator[list[int]]:
    """Collect pids to SIGKILL on teardown, by exact pid, only for ones we spawned.

    When the assertion fails the component really is still running, and a
    300-second sleeper left behind on a shared box is its own kind of mess.
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


def _config() -> ServeConfig:
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
            shutdown_grace_s=1.0,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=1.0,
        components={
            "dispatcher": spec("dispatcher", SLEEPER),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def test_adopted_component_is_terminated_at_shutdown(reaper: list[int]) -> None:
    """Supervisor B adopts a running dispatcher; shutdown() must stop it."""
    first = Supervisor("op", _config())
    assert first.start("dispatcher") is True
    pid = first.children["dispatcher"].pid
    assert pid is not None
    reaper.append(pid)

    # Supervisor A is dropped without shutdown(), exactly as a serve that is
    # SIGKILLed would be: the pid file and the live process both survive.
    del first
    assert pid_alive(pid), "the component must outlive its first supervisor"
    assert component_pid_path("op", "dispatcher").exists(), (
        "the pid file is the whole restart-safety mechanism; it must survive too"
    )

    # Supervisor B is a fresh object over the same serve directory.
    second = Supervisor("op", _config())
    assert second.adopt("dispatcher") is True, "precondition: the re-attach must work"
    assert pid_alive(pid), "precondition: an adopted component is still running"

    second.shutdown()

    assert _wait_gone(pid), (
        f"pid {pid} was adopted by the second supervisor and is still running after "
        "its shutdown(): adopt() records the identity but no Popen, and shutdown() "
        "only iterates self._procs, so a re-attached component is never signalled "
        "and survives serve holding its worktree and gate slot"
    )
