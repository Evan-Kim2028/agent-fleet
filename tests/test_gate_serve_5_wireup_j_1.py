"""A self-killed SIGKILL child must still consume the crash budget.

``Supervisor._reap`` reclassifies a death whose pending cause is a *requested*
one, and the first branch it consults is ``_KILLED_BY_SIGNAL``
(``{-SIGKILL, -SIGINT}``): a child reaped on one of those signals is booked as
``CAUSE_REQUESTED_KILLED`` without ever asking whether serve actually sent that
signal. ``Supervisor.start`` defaults ``cause`` to ``CAUSE_REQUESTED`` and
``tick``'s ensure-running loop calls it with no cause at all, so the very first
spawn of any component is tagged ``requested`` — and an OOM-killed child is
booked as a stop serve asked for.

``_note_crash`` only appends to ``crash_epochs`` when the cause is
``CAUSE_CRASH``, so the crash budget stays permanently empty. The component
restarts forever, the restarts counter climbs without bound, and no
``serve.component.crash_loop`` alert is ever raised — the exact failure the
budget exists to catch. SIGSEGV and a plain non-zero exit classify correctly
because neither appears in ``_KILLED_BY_SIGNAL``, which is what isolates the
defect to the SIGKILL path.

Real child processes throughout: the behaviour under test *is* "this child died
on SIGKILL", which only a real process can report.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.supervisor import (
    STATE_CRASH_LOOPING,
    Supervisor,
)

if TYPE_CHECKING:
    from pathlib import Path

#: The shipped pacing: ServeConfig.tick_seconds defaults to 15.0.
TICK_SECONDS = 15.0
TICKS = 60
#: 60 ticks * 15s = 900s, well inside the 15-minute crash window, so a
#: correctly-booked budget is guaranteed to have tripped long before the run ends.
CRASH_THRESHOLD = 3
CRASH_WINDOW_MINUTES = 15

#: OOM-killer's signature: the child cannot catch it, trap it or log it.
SIGKILLER = f"{sys.executable} -c 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'"
#: A fatal signal serve never sends — must be charged as a crash.
SEGV = f"{sys.executable} -c 'import os, signal; os.kill(os.getpid(), signal.SIGSEGV)'"
#: A non-zero exit the child chose itself — must be charged as a crash.
EXIT_7 = f"{sys.executable} -c 'raise SystemExit(7)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _config(command: str) -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=cmd,
            backoff_initial_s=1.0,
            backoff_max_s=4.0,
            crash_threshold=CRASH_THRESHOLD,
            crash_window_minutes=CRASH_WINDOW_MINUTES,
            no_progress_restarts=2,
            no_progress_window_minutes=30,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=TICK_SECONDS,
        shutdown_grace_s=1.0,
        components={
            "dispatcher": spec("dispatcher", command),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _drive(command: str, ticks: int = TICKS) -> tuple[str, int, int, str]:
    """Tick *ticks* times at the shipped pacing; return the final component state.

    The fake clock advances the schedule, but the child is a real process and
    dies in real time, so each pass first waits (bounded) for the spawn to
    actually exit before reaping it.
    """
    clock = FakeClock()
    sup = Supervisor("op", _config(command), clock=clock)
    sup.defer_restarts = True
    try:
        for _ in range(ticks):
            deadline = time.monotonic() + 5.0
            while sup._procs and time.monotonic() < deadline:
                time.sleep(0.01)
            clock.advance(TICK_SECONDS)
            sup.tick()
        state = sup.children["dispatcher"]
        return state.state, state.restarts, len(state.crash_epochs), state.last_exit_cause
    finally:
        sup.shutdown()


def test_sigkilled_child_is_charged_to_the_crash_budget() -> None:
    """An OOM-killed child is a crash, so the budget must trip and stop it."""
    state, restarts, crash_epochs, cause = _drive(SIGKILLER)

    assert crash_epochs == CRASH_THRESHOLD, (
        "SIGKILL deaths must be charged to the crash budget: serve never requested "
        f"that stop, but _reap booked {crash_epochs} crashes (last cause {cause!r})"
    )
    assert state == STATE_CRASH_LOOPING, (
        f"{CRASH_THRESHOLD} SIGKILL deaths inside {CRASH_WINDOW_MINUTES}m must mark the "
        f"component crash_looping, not leave it in {state!r} with {restarts} restarts"
    )
    assert restarts <= CRASH_THRESHOLD, (
        f"a crash-looping component must stop restarting, but it restarted {restarts} times"
    )


def test_segv_child_still_reaches_crash_looping() -> None:
    """Control: a signal serve never sends classifies correctly today."""
    state, _restarts, crash_epochs, _cause = _drive(SEGV)
    assert crash_epochs == CRASH_THRESHOLD
    assert state == STATE_CRASH_LOOPING


def test_nonzero_exit_child_still_reaches_crash_looping() -> None:
    """Control: a self-chosen non-zero exit classifies correctly today."""
    state, _restarts, crash_epochs, _cause = _drive(EXIT_7)
    assert crash_epochs == CRASH_THRESHOLD
    assert state == STATE_CRASH_LOOPING
