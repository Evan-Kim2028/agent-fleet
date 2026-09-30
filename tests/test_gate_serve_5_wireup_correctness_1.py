"""correctness-1: the no_progress rule must measure idleness, not process age.

``Watchdog.check_no_progress`` reads ``now - state.last_event_epoch``. A grep of
the tree shows the only two writes to that field are in ``Supervisor.start``
(spawn) and ``Supervisor.adopt`` — so it records *when the process was started*,
never *when it last did work*. A component that is alive and working is
therefore indistinguishable from a wedged one, and every watchdog tick past the
window kills and respawns it, destroying in-flight lane work and burning the
restart budget on a component that never needed it.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.supervisor import Supervisor
from agent_fleet.serve.watchdog import RULE_NO_PROGRESS, Watchdog

if TYPE_CHECKING:
    from pathlib import Path

SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _config(**kw: int) -> ServeConfig:
    return ServeConfig(
        operator="op",
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=SLEEPER,
                no_progress_restarts=kw.pop("no_progress_restarts", 2),
                no_progress_window_minutes=30,
            )
        },
        watchdog=WatchdogConfig(
            no_progress_minutes=1,
            max_remediations_per_tick=5,
            stage_timeout_minutes={"lane": 100000},
        ),
        **kw,
    )


def test_last_event_epoch_is_only_written_at_spawn_and_adopt() -> None:
    """The root cause, pinned at the source level.

    Every tick must be able to tell that a component did something. If the only
    writers are spawn/adopt, no amount of component activity can ever move the
    field, and the rule is a process-age detector wearing a progress detector's
    name.
    """
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    try:
        sup.start("dispatcher")
        after_spawn = sup.children["dispatcher"].last_event_epoch

        # Simulate the component doing a great deal of work: many ticks, board
        # transitions, and time passing.
        for _ in range(5):
            clock.advance(30)
            sup.tick()
        assert sup.children["dispatcher"].last_event_epoch == after_spawn, (
            "last_event_epoch never moved, so no_progress measures time since spawn"
        )
    finally:
        sup.shutdown()


def test_a_healthy_live_component_is_not_restarted_while_work_is_queued() -> None:
    """A running component past the window must not be killed every tick."""
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    try:
        sup.start("dispatcher")
        watchdog = Watchdog("op", _config(), sup, clock=clock)
        original = sup.children["dispatcher"].pid

        restarts = 0
        for _ in range(3):
            clock.advance(61)
            report = watchdog.tick(queued_depth=3)
            restarts += len([r for r in report.remediations if r.rule == RULE_NO_PROGRESS])

        assert restarts == 0, (
            f"a live dispatcher was restarted {restarts} times purely for passing "
            f"time; pid {original} -> {sup.children['dispatcher'].pid}"
        )
        assert sup.children["dispatcher"].pid == original
    finally:
        sup.shutdown()


def test_component_activity_keeps_it_out_of_the_no_progress_rule() -> None:
    """The signal the rule needs is component activity, and it must be readable."""
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    try:
        sup.start("dispatcher")
        state = sup.children["dispatcher"]

        # Something happens: a fresh event is stamped on the child.
        state.last_event_epoch = clock.time()
        clock.advance(30)

        report = Watchdog("op", _config(), sup, clock=clock).tick(queued_depth=3)
        assert [r for r in report.remediations if r.rule == RULE_NO_PROGRESS] == [], (
            "a component that reported an event 30s ago is not wedged"
        )
    finally:
        sup.shutdown()


def test_the_reported_idle_time_is_time_since_the_last_event_not_since_spawn() -> None:
    """A single tick well inside the window after a fresh event must be quiet."""
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    try:
        sup.start("dispatcher")
        state = sup.children["dispatcher"]

        # Pretend the component reported activity right before "now".
        state.last_event_epoch = clock.time()
        clock.advance(1)  # 1 second, far inside the 1-minute window

        report = Watchdog("op", _config(), sup, clock=clock).tick(queued_depth=3)
        assert [r for r in report.remediations if r.rule == RULE_NO_PROGRESS] == []
    finally:
        sup.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
