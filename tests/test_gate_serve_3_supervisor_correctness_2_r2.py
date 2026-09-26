"""correctness-2: shutdown() never applies ServeConfig.shutdown_grace_s.

Config says: "Seconds the supervisor waits for children to exit cleanly on
shutdown, before escalating to a group KILL" (config.py:128-130). The code
reads that field into ``grace`` (supervisor.py:741) and then does not use it for
the stop itself -- the first loop passes ``RESTART_KILL_GRACE_S`` (0.0,
supervisor.py:89) to ``stop_component`` (supervisor.py:747), whose
``_await_gone`` polls for one 20ms tick and escalates to a group SIGKILL.

So a component with a SIGTERM handler that has real work to do is killed with
zero grace, and the second loop's ``_await_gone(..., grace_s=grace)``
(supervisor.py:749) is a no-op on a proc that is already dead and reaped.

Measured at the head: ``shutdown()`` returns in ~0.00s, the child's handler
never completes, and the child dies of SIGKILL (returncode -9) rather than
exiting 0.
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
from agent_fleet.serve.supervisor import Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: SIGTERM handler that sleeps 1s before writing its marker and exiting 0.
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

GRACE_S = 5.0
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


def test_shutdown_grants_the_configured_grace_to_a_terminating_child(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cleanup.marker"
    ready = tmp_path / "cleanup.marker.ready"
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

        assert marker.exists(), (
            f"shutdown() returned in {elapsed:.2f}s and the component's SIGTERM handler "
            f"never finished, so its cleanup was lost; shutdown_grace_s={GRACE_S} was "
            f"never granted (the child is now gone with returncode {returncode})"
        )
        assert elapsed >= CLEANUP_S, (
            f"shutdown() blocked for {elapsed:.2f}s, less than the {CLEANUP_S}s the child "
            f"needed; the group KILL fired before the configured {GRACE_S}s grace expired"
        )
        assert returncode == 0, (
            f"the component exited {returncode} rather than 0, i.e. it was SIGKILLed "
            f"instead of being given its {GRACE_S}s grace"
        )
    finally:
        sup.shutdown()
        if pid is not None:
            with suppress(OSError):
                os.kill(pid, 9)
