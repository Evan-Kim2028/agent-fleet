"""prodsafety-2: tick() must not block the supervisor loop on the restart backoff.

``tick()`` calls ``self.clock.sleep(delay)`` inline (supervisor.py:533) for
every exit that owes a restart. A crashing component therefore blocks the whole
loop: no other component is reaped, no restart is evaluated, and ``serve
status`` goes stale. The signal handler installed by ``reaper_signals`` only
sets ``self._stopping``, which is not observed until ``tick`` returns -- and
``time.sleep`` resumes across a handled signal (PEP 475), so a supervisor parked
in a saturated backoff cannot act on SIGTERM for the remainder of the sleep.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.supervisor import STATE_BACKOFF, Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CRASHER = f"{sys.executable} -c 'import sys; sys.exit(3)'"
SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"


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


class _RecordingClock:
    """A clock that records sleeps instead of performing them."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def time(self) -> float:
        return 1_000_000.0 + sum(self.slept)

    def monotonic(self) -> float:
        return sum(self.slept)

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def advance(self, seconds: float) -> None:
        self.slept.append(seconds)


def _config(*, backoff_s: float, crash_threshold: int, tick_seconds: float) -> ServeConfig:
    def spec(name: str, command: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=command,
            backoff_initial_s=backoff_s,
            backoff_max_s=backoff_s,
            crash_threshold=crash_threshold,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=tick_seconds,
        shutdown_grace_s=0.2,
        components={
            "dispatcher": spec("dispatcher", None),
            "merger": spec("merger", CRASHER),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def test_a_single_tick_sleeps_the_whole_backoff_inline() -> None:
    """One tick spends the entire restart delay before anything else happens."""
    clock = _RecordingClock()
    sup = Supervisor(
        "op", _config(backoff_s=7.0, crash_threshold=99, tick_seconds=0.05), clock=clock
    )
    try:
        sup.tick()  # spawns merger, which immediately exits 3
        proc = sup._procs["merger"]
        assert _wait_until(lambda: proc.poll() is not None), "merger never exited"

        clock.slept.clear()
        sup.tick()  # reaps merger's crash and owes a restart

        assert clock.slept == [7.0], (
            f"tick() slept {clock.slept} inline instead of deferring the backoff "
            "to the caller; one crashing component parks the whole supervisor"
        )
        assert sup.children["merger"].state == STATE_BACKOFF
    finally:
        sup.shutdown()


def test_the_backoff_delay_is_much_larger_than_the_tick_cadence() -> None:
    """The real-clock cost: a tick that should take milliseconds takes seconds."""
    sup = Supervisor(
        "op", _config(backoff_s=2.0, crash_threshold=99, tick_seconds=0.05), clock=SystemClock()
    )
    try:
        sup.tick()
        proc = sup._procs["merger"]
        assert _wait_until(lambda: proc.poll() is not None), "merger never exited"

        durations: list[float] = []
        for _ in range(3):
            started = time.monotonic()
            sup.tick()
            durations.append(time.monotonic() - started)
            proc = sup._procs.get("merger")
            if proc is not None:
                assert _wait_until(lambda p=proc: p.poll() is not None), "merger never exited"

        worst = max(durations)
        assert worst < 1.0, (
            f"a tick blocked for {worst:.2f}s against a 0.05s cadence; during that "
            "window no other component is reaped and SIGTERM cannot be acted on"
        )
    finally:
        sup.shutdown()


def test_reaper_signals_do_not_cut_a_sleeping_tick_short() -> None:
    """``_stopping`` is only checked after the inline sleep returns."""
    clock = _RecordingClock()
    sup = Supervisor(
        "op", _config(backoff_s=7.0, crash_threshold=99, tick_seconds=0.05), clock=clock
    )
    try:
        sup.tick()
        proc = sup._procs["merger"]
        assert _wait_until(lambda: proc.poll() is not None), "merger never exited"

        sup._stopping = True  # what the SIGTERM handler does
        clock.slept.clear()
        sup.tick()

        assert clock.slept == [], (
            f"a stopping supervisor still slept {clock.slept}; the sleep happens "
            "before the _stopping check, so a stop is delayed by the full backoff"
        )
    finally:
        sup.shutdown()
