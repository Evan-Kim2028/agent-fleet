"""spec-1: shutdown() must use the configured grace, not the restart grace.

``Supervisor.shutdown`` reads ``self.config.shutdown_grace_s`` into ``grace``
(supervisor.py:741) and then stops every component with
``grace_s=RESTART_KILL_GRACE_S`` (supervisor.py:747) -- a constant of ``0.0``
whose only documented meaning is the *restart* path ("Zero means 'no grace': the
old child is already dead", supervisor.py:86-89).

The consequence is that the operator's grace is never honoured: the TERM is
followed by ``escalate_kill_group`` on the first 20ms poll, so a component whose
SIGTERM handler needs a second to write its final state is SIGKILLed mid-write.
The later call at supervisor.py:749 that does pass the real grace cannot help --
the child is already dead and reaped, so ``_await_gone`` returns on its first
``poll()``.
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

#: SIGTERM handler that sleeps 1s, writes .clean, and exits 0.
CLEANER = (
    f"{sys.executable} -c 'import pathlib, signal, sys, time\n"
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

GRACE_S = 10.0
CLEANUP_S = 1.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


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


def test_shutdown_honours_the_operator_grace_instead_of_the_restart_grace(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "clean"
    ready = tmp_path / "clean.ready"
    command = f"{CLEANER} {marker} {CLEANUP_S}"
    assert not any(ch.isspace() for ch in str(marker))

    sup = Supervisor("op", _config(command), clock=SystemClock())
    pid: int | None = None
    try:
        assert sup.start("dispatcher") is True
        proc = sup._procs["dispatcher"]
        pid = proc.pid
        assert _wait_until(ready.exists), "the child never installed its SIGTERM handler"

        started = time.monotonic()
        sup.shutdown()
        elapsed = time.monotonic() - started
        time.sleep(0.2)  # let a surviving handler land its marker
        wrote = marker.exists()

        assert wrote, (
            f"shutdown() returned in {elapsed:.2f}s and the .clean file was never written: "
            f"the child was SIGKILLed on the restart grace "
            f"RESTART_KILL_GRACE_S={RESTART_KILL_GRACE_S} instead of being given the "
            f"configured shutdown_grace_s={GRACE_S} to finish on its own"
        )
        assert elapsed >= CLEANUP_S, (
            f"shutdown() spent {elapsed:.2f}s, less than the {CLEANUP_S}s the child needed, "
            f"so the configured {GRACE_S}s grace was never applied"
        )
    finally:
        sup.shutdown()
        if pid is not None:
            with suppress(OSError):
                os.kill(pid, 9)
