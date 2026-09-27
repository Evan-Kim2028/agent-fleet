"""correctness-4: tick() must not block the supervisor for the whole backoff.

``tick()`` sets the restart deadline and then pays the same delay inline:

    state.restart_due = self.clock.time() + delay     # supervisor.py:580
    ...
    self.clock.sleep(delay)                           # supervisor.py:582

With ``SystemClock.sleep`` that is ``time.sleep(delay)``, so one crash-looping
component stalls the entire watchdog for the full backoff -- up to
``backoff_max_s`` (300s by default) -- and the deadline set one line earlier is
moot, because the loop has already waited it out. The module's headline promise
is "One crash-looping component does not take the fleet down"; while a tick is
inside that sleep, no other component is reaped, no watchdog rule runs and
``serve status`` cannot answer.

The deadline is the mechanism that is supposed to make this non-blocking: with
``backoff_initial_s=3.0`` and ``tick_seconds=15.0``, a correct tick returns
immediately and leaves the component in backoff until its deadline passes.
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
from agent_fleet.serve.supervisor import STATE_BACKOFF, Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Exits 3 at once -- a crash the supervisor owes a restart for.
CRASHER = f"{sys.executable} -c 'import sys; sys.exit(3)'"

BACKOFF_INITIAL_S = 3.0
BACKOFF_MAX_S = 300.0
TICK_SECONDS = 15.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _config() -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=cmd,
            backoff_initial_s=BACKOFF_INITIAL_S,
            backoff_max_s=BACKOFF_MAX_S,
            crash_threshold=5,
            crash_window_minutes=15,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=TICK_SECONDS,
        shutdown_grace_s=1.0,
        components={
            "dispatcher": spec("dispatcher", CRASHER),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_one_tick_does_not_block_for_the_whole_restart_backoff() -> None:
    sup = Supervisor("op", _config(), clock=SystemClock())
    try:
        assert sup.start("dispatcher") is True
        first_pid = sup._procs["dispatcher"].pid
        assert _wait_until(lambda: sup._procs["dispatcher"].poll() is not None), (
            "precondition: the component has already crashed"
        )

        started = time.monotonic()
        sup.tick()
        elapsed = time.monotonic() - started

        state = sup.children["dispatcher"]
        assert elapsed < 1.0, (
            f"one tick() blocked for {elapsed:.2f}s on a single crash-looping component: "
            f"tick() pays backoff_initial_s={BACKOFF_INITIAL_S} inline via clock.sleep "
            f"(supervisor.py:582) instead of leaving it to the restart_due deadline it "
            f"just set (supervisor.py:580). At backoff_max_s={BACKOFF_MAX_S} the whole "
            f"supervisor -- watchdog rules and serve status included -- is stuck for "
            f"5 minutes per crash-looping component."
        )
        assert state.state == STATE_BACKOFF, (
            f"after a non-blocking tick the component should still be waiting on its "
            f"deadline, but its state is {state.state!r} (restart_due="
            f"{state.restart_due}, now={sup.clock.time()}); the inline sleep already "
            f"waited the {BACKOFF_INITIAL_S}s out and respawned it"
        )
        assert first_pid not in {proc.pid for proc in sup._procs.values()}, (
            "the replacement was spawned inside the same tick, so the deadline set on "
            "line 580 was already spent rather than honoured"
        )
    finally:
        pids = [proc.pid for proc in sup._procs.values()]
        sup.shutdown()
        for pid in pids:
            with suppress(OSError):
                os.kill(pid, 9)
