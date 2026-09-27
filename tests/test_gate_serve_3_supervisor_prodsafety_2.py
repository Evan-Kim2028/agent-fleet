"""prodsafety-2: shutdown() SIGKILLs every component after ~20ms.

``shutdown()``'s first loop (supervisor.py:744-747) stops each component with
``grace_s=RESTART_KILL_GRACE_S``, which is ``0.0`` (supervisor.py:89). That is
the *restart* grace -- the code path comment says zero there means "the old
child is already dead" -- but on the shutdown path it means the operator's
``shutdown_grace_s`` (default 10s) is never granted to anything.

``_await_gone`` then polls ``time.sleep(0.02)`` once and escalates with
``escalate_kill_group``. A component whose SIGTERM handler flushes state, closes
a worktree or releases a lock is killed ~20ms into that handler, so the work is
lost every time, and the second loop's real-grace call
(supervisor.py:749) finds an already-reaped proc.

The test holds the child alive after ``shutdown()`` returns and checks its
handler ran -- the child must still be alive inside the grace, and dead only
after it.
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

#: SIGTERM handler that takes 3s of cleanup before writing its marker.
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
CLEANUP_S = 3.0


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


def test_shutdown_lets_a_component_finish_its_sigterm_handler(tmp_path: Path) -> None:
    marker = tmp_path / "flushed.marker"
    ready = tmp_path / "flushed.marker.ready"
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
        returncode = proc.returncode
        time.sleep(0.2)  # give a surviving handler room to land its marker
        marker_written = marker.exists()

        assert marker_written, (
            f"the component's SIGTERM handler never finished: shutdown() returned in "
            f"{elapsed:.2f}s with shutdown_grace_s={GRACE_S} configured, so the TERM was "
            f"followed ~20ms later by a group KILL on the restart grace "
            f"RESTART_KILL_GRACE_S={RESTART_KILL_GRACE_S} and the state flush was lost "
            f"(returncode {returncode})"
        )
        assert elapsed >= CLEANUP_S, (
            f"shutdown() spent {elapsed:.2f}s, less than the {CLEANUP_S}s of cleanup the "
            f"component was doing; the configured {GRACE_S}s grace was never granted"
        )
        assert returncode == 0, (
            f"the component died with returncode {returncode} instead of exiting 0: it was "
            f"SIGKILLed rather than given the {GRACE_S}s grace to finish on its own"
        )
    finally:
        sup.shutdown()
        if pid is not None:
            with suppress(OSError):
                os.kill(pid, 9)
