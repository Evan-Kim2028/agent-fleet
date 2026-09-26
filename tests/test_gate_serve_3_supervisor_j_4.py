"""j_4: a persisted restart deadline must be rebased when the clock steps back.

``ChildState.restart_due`` is the deadline a restart waits for, and it is
persisted verbatim as a raw wall-clock epoch (:meth:`ChildState.to_dict`).
:meth:`Supervisor._restore` loads it straight back onto a clock the new
supervisor has never checked, and :meth:`Supervisor._ensure_running` gates on
``state.restart_due > now`` with ``now = self.clock.time()``. Nothing anywhere
in the file rebases, clamps, or clears that deadline.

So whenever the new supervisor's wall clock reads *earlier* than the epoch the
state file was written with — an NTP step backwards, a host resume, a serve
directory restored from a backup — every component that was mid-backoff is
parked for far longer than the backoff it owed, on every tick, forever. The
component is never started again and ``serve status`` reports ``backoff`` with
no pid.

This contradicts the rules the same codebase states. ``clock.py`` says "the
supervisor's own backoff must use monotonic", and the comment in
:meth:`Supervisor.tick` claims the wait is taken on the injected clock "so the
backoff schedule is driven by a test in microseconds and by real seconds in
production" and that a restarting supervisor "resumes the same schedule". A
wall-clock deadline that a backwards step silently extends is neither.

The test stages exactly that: a previous supervisor persisted a 60-second
backoff deadline at a known epoch, and a replacement supervisor's wall clock
reads 300 seconds *earlier*. The deadline is now 360 seconds out instead of 60,
and no number of ticks is going to bring it forward, because nothing in the
code ever moves it.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.paths import state_path, write_json_atomic
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.supervisor import STATE_BACKOFF, Supervisor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: A component that would run indefinitely if it were ever started.
SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"

#: The wall-clock epoch a previous supervisor read when it wrote the state file.
WRITTEN_AT = 2_000_000.0

#: The backoff it had actually earned and owed: a real, finite 60 seconds.
OWED_BACKOFF_S = 60.0

#: How far the replacement supervisor's wall clock reads *earlier* than
#: ``WRITTEN_AT`` — an NTP step backwards, a resumed host, a restored directory.
CLOCK_STEP_BACK_S = 300.0

#: Ticks driven on the replacement supervisor. Every one of them should have
#: started the component had the deadline been rebased onto this clock.
TICKS = 10


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


@pytest.fixture
def reaper() -> Iterator[list[int]]:
    """SIGKILL a replacement child on teardown, by exact pid, only ours.

    If the deadline ever is rebased the component really does start, and a
    300-second sleeper left behind on a shared box is its own kind of mess.
    """
    pids: list[int] = []
    try:
        yield pids
    finally:
        for pid in pids:
            if pid > 0 and pid_alive(pid):
                os.kill(pid, 9)


def _config() -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=cmd,
            backoff_initial_s=5.0,
            backoff_max_s=60.0,
            # A budget the component has not spent: the parking below is the
            # persisted deadline, not a crash loop.
            crash_threshold=1000,
            crash_window_minutes=15,
            no_progress_restarts=2,
            no_progress_window_minutes=30,
            shutdown_grace_s=1.0,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=15.0,
        shutdown_grace_s=1.0,
        components={
            "dispatcher": spec("dispatcher", SLEEPER),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _write_backoff_state() -> None:
    """Persist what a previous supervisor left behind: a dispatcher mid-backoff."""
    write_json_atomic(
        state_path("op"),
        {
            "operator": "op",
            "updated_epoch": WRITTEN_AT,
            "children": {
                "dispatcher": {
                    "name": "dispatcher",
                    "state": STATE_BACKOFF,
                    "pid": None,
                    "starttime": None,
                    "restarts": 3,
                    "crash_epochs": [],
                    "last_exit_epoch": WRITTEN_AT,
                    "last_exit_cause": "crash",
                    "last_exit_code": 1,
                    "pending_cause": "exit",
                    "restart_due": WRITTEN_AT + OWED_BACKOFF_S,
                    "adopted": False,
                    "last_event_epoch": WRITTEN_AT,
                    "no_progress_restarts": [],
                    "message": f"restarting in {OWED_BACKOFF_S:.0f}s",
                }
            },
        },
    )


def test_backward_clock_step_does_not_strand_a_component_in_backoff(reaper: list[int]) -> None:
    """A deadline written before a backwards clock step must not park the component."""
    _write_backoff_state()

    # The replacement supervisor's wall clock reads behind the epoch the state
    # file was written with.
    clock = FakeClock(start_time=WRITTEN_AT - CLOCK_STEP_BACK_S)
    sup = Supervisor("op", _config(), clock=clock)
    state = sup.children["dispatcher"]

    # What the owed backoff is worth on this clock: the deadline is the written
    # one, so the step-back is added on top of the backoff it had earned.
    owed_now = state.restart_due - clock.time()
    assert owed_now == OWED_BACKOFF_S + CLOCK_STEP_BACK_S, (
        f"precondition: the restored deadline should read {owed_now}s away "
        f"({OWED_BACKOFF_S}s owed + {CLOCK_STEP_BACK_S}s of clock step-back)"
    )

    for _ in range(TICKS):
        sup.tick()

    if state.pid is not None:
        reaper.append(state.pid)

    row = {r["component"]: r for r in sup.status_rows()}["dispatcher"]
    assert row["state"] == "running" and row["pid"] is not None, (
        f"after {TICKS} ticks the dispatcher is still {row['state']!r} with pid "
        f"{row['pid']!r}, its {owed_now:.0f}s-away deadline unmoved. restart_due is "
        f"persisted as a raw wall-clock epoch and loaded verbatim by _restore(); "
        f"_ensure_running() gates on `restart_due > self.clock.time()` and nothing "
        f"rebases or clears it, so a wall clock reading behind the one the state file "
        f"was written with parks a mid-backoff component forever and serve status "
        f"reports backoff with no process running"
    )
