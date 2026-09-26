"""prodsafety-1: the no-progress rule must not restart a healthy, working component.

The production-safety framing of the ``last_event_epoch`` defect: the field the
rule reads is only ever written at spawn (``Supervisor.start``) and adopt
(``Supervisor.adopt``), so ``idle_s`` is "seconds since the component process
launched", not "seconds since it last said anything". A component that is
actively working, holds one queued item, and has been up longer than
``no_progress_minutes`` is therefore declared wedged and restarted.

The second half is worse than the churn. Each restart re-arms
``last_event_epoch`` to now, so restarts land one full window apart; the burst
budget is measured backwards from the newest restart, which pins the count at 1;
and with a budget of 2 the escalate branch is never selected. The mechanism whose
job is to stop the restarts never fires, and a wedged component is restarted
forever.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.supervisor import Supervisor
from agent_fleet.serve.watchdog import RULE_NO_PROGRESS, Watchdog

if TYPE_CHECKING:
    from pathlib import Path

SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _config() -> ServeConfig:
    """Shipped ratios, scaled down so the rule fires inside a bounded test."""
    return ServeConfig(
        operator="op",
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=SLEEPER,
                no_progress_restarts=2,
                no_progress_window_minutes=30,
            )
        },
        watchdog=WatchdogConfig(
            no_progress_minutes=1,
            # Keep the other rules from acting so only no_progress is observed.
            stage_timeout_minutes={"lane": 10_000},
            orphan_minutes=10_000,
            stale_lock_minutes=10_000,
            deadlock_minutes=10_000,
        ),
    )


def _busy_log(clock: FakeClock) -> None:
    """The component is demonstrably working: it wrote output just now."""
    import os

    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("lane running\n", encoding="utf-8")
    os.utime(log, (clock.time(), clock.time()))


def _run_cycles(*, cycles: int, window_s: float) -> tuple[list[str], list[int]]:
    """Advance one window per cycle, the component working throughout."""
    clock = FakeClock()
    config = _config()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)
        actions: list[str] = []
        pids: list[int] = []
        for _ in range(cycles):
            clock.advance(window_s)
            _busy_log(clock)
            report = watchdog.tick(queued_depth=1)
            actions.extend(r.action for r in report.remediations if r.rule == RULE_NO_PROGRESS)
            pids.append(sup.children["dispatcher"].pid)
        return actions, pids
    finally:
        sup.shutdown()


def test_a_working_component_with_queued_work_is_not_restarted() -> None:
    """The correct outcome: no restart, for a component that is plainly alive.

    One item is queued, so the rule's precondition holds and the rule is entitled
    to judge the component. It wrote output within the window, so there is no
    evidence of a stall.
    """
    clock = FakeClock()
    config = _config()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)
        pid = sup.children["dispatcher"].pid
        for _ in range(3):
            clock.advance(600)  # ten windows, the component working throughout
            _busy_log(clock)
            report = watchdog.tick(queued_depth=1)
            assert [r for r in report.remediations if r.rule == RULE_NO_PROGRESS] == [], (
                f"a live, actively working component was restarted after being up "
                f"600s: {report.remediations}"
            )
        assert sup.children["dispatcher"].pid == pid
        assert pid_alive(pid) is True
    finally:
        sup.shutdown()


def test_repeated_windows_restart_a_healthy_component_forever() -> None:
    """The production consequence: a healthy component is churned, not healed.

    The escalation path that is supposed to stop the restarts never fires, so
    this is not a bounded number of restarts — it is an unbounded loop.
    """
    actions, pids = _run_cycles(cycles=5, window_s=600)
    assert "escalate" in actions, (
        f"five consecutive no-progress windows on a working component produced "
        f"{actions} and never escalated; each restart re-arms last_event_epoch, so "
        f"restarts land one window apart, the burst count stays at 1, and the "
        f"escalation that is supposed to stop the loop is unreachable"
    )
    assert len(set(pids)) == 1, f"the component was replaced {len(set(pids)) - 1} times: {pids}"


def test_last_event_epoch_does_not_track_component_activity() -> None:
    """The field is pinned to the process start epoch and never moves.

    ``Supervisor.start`` and ``Supervisor.adopt`` are the only writers, so no
    amount of component activity can make ``idle_s`` go back down.
    """
    clock = FakeClock()
    config = _config()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        state = sup.children["dispatcher"]
        start_epoch = state.last_event_epoch

        for _ in range(3):
            clock.advance(120)
            _busy_log(clock)
            assert state.last_event_epoch == start_epoch, (
                "last_event_epoch moved without a third writer, so the rule cannot "
                "tell a working component from a silent one"
            )
        idle = clock.time() - state.last_event_epoch
        assert idle == clock.time() - start_epoch
    finally:
        sup.shutdown()
