"""spec-2: the stage-retry budget must be windowed, not a process-lifetime counter.

The module docstring promises, as one of its three load-bearing constraints:

    **Fail closed, then let the owner retry.** A stuck stage is killed and marked
    dead rather than left running. "Dead" means the owning component may retry it
    once (budget in config) and then must escalate.

The budget lives in ``Watchdog.stage_retries`` — a dict created in ``__init__``
and only ever incremented. It is not windowed, not time-based, and never reset.
With the shipped ``stage_retry_budget=1`` a single transient incident therefore
disables the ``terminate_group`` remediation *for the rest of the serve process's
life*: the rule degrades into ``alert()`` on every tick while the wedged process
keeps running, which is precisely the outcome the docstring says must not happen.

The rule the code itself gets right two dozen lines away is the shape of the fix:
``check_no_progress`` measures its budget over ``no_progress_window_minutes``.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import LEVEL_ERROR, read_serve_events
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.supervisor import Supervisor
from agent_fleet.serve.watchdog import RULE_STUCK_STAGE, Watchdog

if TYPE_CHECKING:
    from pathlib import Path

SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _shipped() -> ServeConfig:
    """Shipped defaults for the two settings that matter here."""
    return ServeConfig(
        operator="op",
        components={"dispatcher": ComponentSpec(name="dispatcher", command=SLEEPER)},
        watchdog=WatchdogConfig(
            stage_timeout_minutes={"lane": 1},
            stage_retry_budget=1,
            no_progress_minutes=10_000,
            orphan_minutes=10_000,
            stale_lock_minutes=10_000,
            deadlock_minutes=10_000,
        ),
    )


def _write_log(clock: FakeClock, text: str, *, age_s: float) -> None:
    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    stamp = clock.time() - age_s
    os.utime(log, (stamp, stamp))


def _recover(clock: FakeClock, sup: Supervisor) -> None:
    time.sleep(0.2)
    sup.tick()
    sup.start("dispatcher")
    sup.children["dispatcher"].last_event_epoch = clock.time()
    _write_log(clock, "healthy\n", age_s=0.0)


def _stuck_action(report) -> str | None:  # noqa: ANN001
    stuck = [r for r in report.remediations if r.rule == RULE_STUCK_STAGE]
    return stuck[0].action if stuck else None


def test_the_second_wedge_of_the_process_life_is_still_killed() -> None:
    """The correct outcome: a genuinely stuck stage is killed, not just alerted.

    Incident 1 is transient, recovers, and the component works for hours.
    Incident 2 is an unrelated, genuinely wedged stage 10000s later. The
    docstring's guarantee is per episode, so it must be killed.
    """
    clock = FakeClock()
    config = _shipped()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)

        _write_log(clock, "wedged\n", age_s=3600)
        assert _stuck_action(watchdog.tick()) == "terminate_group"

        _recover(clock, sup)
        clock.advance(10_000)
        _write_log(clock, "healthy again\n", age_s=0.0)
        assert _stuck_action(watchdog.tick()) is None, "precondition: recovered"

        _write_log(clock, "wedged for good\n", age_s=3600)
        wedged_pid = sup.children["dispatcher"].pid
        report = watchdog.tick()

        assert _stuck_action(report) == "terminate_group", (
            f"the second genuinely stuck stage of this serve process's life was not "
            f"killed; got {_stuck_action(report)!r}. stage_retries="
            f"{watchdog.stage_retries} is a bare counter that is only ever "
            f"incremented, so the one allowed retry was consumed by the first incident"
        )
        assert pid_alive(wedged_pid) is False
    finally:
        sup.shutdown()


def test_the_retry_budget_is_windowed_so_old_incidents_age_out() -> None:
    """The bookkeeping the fix requires, stated directly.

    ``check_no_progress`` measures its budget over ``no_progress_window_minutes``;
    the stuck-stage budget must not be a bare lifetime tally.
    """
    clock = FakeClock()
    config = _shipped()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)
        _write_log(clock, "wedged\n", age_s=3600)
        watchdog.tick()
        assert watchdog.stage_retries.get("dispatcher", 0) == 1

        _recover(clock, sup)
        clock.advance(10_000)
        _write_log(clock, "healthy\n", age_s=0.0)
        watchdog.tick()

        assert watchdog.stage_retries.get("dispatcher", 0) == 0, (
            "the retry spent 10000s ago is still counted against this stage; a "
            "per-window counter mirroring no_progress_window_minutes is what the "
            "module already uses for the other rule"
        )
    finally:
        sup.shutdown()


def test_a_spent_budget_emits_an_error_alert_every_tick() -> None:
    """The operator-visible half: the alert storm that replaces the kill.

    Once the budget is spent the rule alerts on every tick and never acts, so the
    self-healing guarantee is replaced by a stream of identical errors.
    """
    clock = FakeClock()
    config = _shipped()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)
        _write_log(clock, "wedged\n", age_s=3600)
        watchdog.tick()
        _recover(clock, sup)
        clock.advance(10_000)

        _write_log(clock, "wedged for good\n", age_s=3600)
        wedged_pid = sup.children["dispatcher"].pid
        before = len(read_serve_events("op"))
        for _ in range(5):
            _write_log(clock, "wedged for good\n", age_s=3600)
            watchdog.tick()
            clock.advance(60)
        alerts = [
            e
            for e in read_serve_events("op")[before:]
            if e.get("event") == "serve.watchdog.stage_dead" and e.get("level") == LEVEL_ERROR
        ]

        assert len(alerts) <= 1, (
            f"{len(alerts)} error-level alerts for one unchanged wedged stage in 5 "
            f"ticks; the escalate branch alerts unconditionally with no latch"
        )
        assert pid_alive(wedged_pid) is False, (
            "the wedged process is still running after the budget was spent by an "
            "unrelated earlier incident, contradicting the docstring's 'A stuck "
            "stage is killed and marked dead rather than left running'"
        )
    finally:
        sup.shutdown()
