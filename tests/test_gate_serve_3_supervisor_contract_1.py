"""contract-1: shutdown() must honour ServeConfig.shutdown_grace_s.

``ServeConfig.shutdown_grace_s`` is documented (config.py:128-130) as "Seconds
the supervisor waits for children to exit cleanly on shutdown, before
escalating to a group KILL", and ``shutdown()`` reads it into ``grace`` -- but
then the *first* loop calls ``stop_component(name, grace_s=RESTART_KILL_GRACE_S)``
(supervisor.py:747), and ``RESTART_KILL_GRACE_S`` is ``0.0`` (supervisor.py:89).

``stop_component`` ends in ``_await_gone(..., grace_s=0.0)``, whose loop polls
``time.sleep(0.02)`` and then, on deadline, calls ``escalate_kill_group``. So the
configured grace is spent zero times: TERM is followed by a group SIGKILL ~20ms
later, and the child never finishes whatever its SIGTERM handler was doing.

The second loop (supervisor.py:749) does pass the real grace, but by then the
child is already dead and reaped, so ``_await_gone`` returns on its first
``poll()`` -- which is why the second call makes the grace field dead config
rather than the working path.

The component here traps SIGTERM, takes 3s of cleanup, then writes a marker.
With ``shutdown_grace_s=30.0`` the child must be allowed to finish.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.supervisor import RESTART_KILL_GRACE_S, Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Installs a SIGTERM handler, announces readiness, then takes CLEANUP_S to
#: finish. The marker path arrives as argv[1].
CLEANER = (
    f"{sys.executable} -c 'import os, pathlib, signal, sys, time\n"
    f"marker = pathlib.Path(sys.argv[1])\n"
    f"delay = float(sys.argv[2])\n"
    f"def _bye(sig, frame):\n"
    f"    time.sleep(delay)\n"
    f'    marker.write_text("clean", encoding="utf-8")\n'
    f"    sys.exit(0)\n"
    f"signal.signal(signal.SIGTERM, _bye)\n"
    f'pathlib.Path(str(marker) + ".ready").write_text("armed", encoding="utf-8")\n'
    f"time.sleep(300)'"
)

GRACE_S = 30.0
CLEANUP_S = 3.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _command(marker: Path) -> str:
    assert not any(ch.isspace() for ch in str(marker)), "argv token must be shell-split clean"
    return f"{CLEANER} {marker} {CLEANUP_S}"


def _config(command: str) -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(name=name, command=cmd)

    return ServeConfig(
        operator="op",
        tick_seconds=15.0,
        shutdown_grace_s=GRACE_S,
        components={
            "dispatcher": spec("dispatcher", command),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_shutdown_waits_out_the_configured_grace_instead_of_group_killing(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cleaned.marker"
    ready = tmp_path / "cleaned.marker.ready"
    sup = Supervisor("op", _config(_command(marker)), clock=SystemClock())
    pid: int | None = None
    try:
        assert sup.start("dispatcher") is True
        proc = sup._procs["dispatcher"]
        pid = proc.pid
        assert _wait_until(ready.exists), "the child never installed its SIGTERM handler"

        started = time.monotonic()
        sup.shutdown()
        elapsed = time.monotonic() - started

        # A correct shutdown pays the child's cleanup and the marker is written.
        assert marker.exists(), (
            f"the child never finished its SIGTERM cleanup: shutdown() returned after "
            f"{elapsed:.2f}s with shutdown_grace_s={GRACE_S}, so the TERM was followed by "
            f"a group KILL (RESTART_KILL_GRACE_S={RESTART_KILL_GRACE_S}) and the marker "
            f"was never written"
        )
        assert elapsed >= CLEANUP_S, (
            f"shutdown() returned after {elapsed:.2f}s, less than the child's "
            f"{CLEANUP_S}s of cleanup work; the configured {GRACE_S}s grace was never paid"
        )
    finally:
        sup.shutdown()
        if pid is not None:
            with suppress(OSError):
                os.kill(pid, 9)
