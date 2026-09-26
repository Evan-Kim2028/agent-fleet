"""correctness-1: the stage-retry budget must be per stuck *episode*, not per process.

The module docstring states the contract this breaks:

    **Fail closed, then let the owner retry.** A stuck stage is killed and marked
    dead rather than left running. "Dead" means the owning component may retry it
    once (budget in config) and then must escalate.

The budget is enforced by ``Watchdog.stage_retries``, a plain dict initialised
once in ``__init__`` and only ever *incremented* in ``check_stuck_stages``. It is
never reset when a stage recovers. So the budget is consumed once per serve
process lifetime rather than once per incident: a transient wedge early in the
process's life spends the single retry, and every later genuinely stuck stage
finds the budget spent and degrades to an ``escalate`` alert while the wedged
process keeps running.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import read_serve_events
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


def _config(*, stage_retry_budget: int = 1) -> ServeConfig:
    return ServeConfig(
        operator="op",
        components={"dispatcher": ComponentSpec(name="dispatcher", command=SLEEPER)},
        watchdog=WatchdogConfig(
            stage_timeout_minutes={"lane": 1},
            stage_retry_budget=stage_retry_budget,
            # Keep the other rules out of the way.
            orphan_minutes=10_000,
            stale_lock_minutes=10_000,
            deadlock_minutes=10_000,
            no_progress_minutes=10_000,
        ),
    )


def _write_log(clock: FakeClock, text: str, *, age_s: float = 0.0) -> Path:
    """Write the component's log and stamp it *age_s* in the past."""
    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    stamp = clock.time() - age_s
    os.utime(log, (stamp, stamp))
    return log


def _recovered(clock: FakeClock, sup: Supervisor) -> None:
    """Let the stage recover: a fresh, healthy component with a growing log."""
    time.sleep(0.2)
    sup.tick()
    sup.start("dispatcher")
    sup.children["dispatcher"].last_event_epoch = clock.time()
    _write_log(clock, "recovered\n")


def _stuck_action(report) -> str | None:  # noqa: ANN001
    stuck = [r for r in report.remediations if r.rule == RULE_STUCK_STAGE]
    return stuck[0].action if stuck else None


def test_a_second_stuck_episode_after_a_recovery_is_terminated() -> None:
    """The correct outcome: a fresh stuck episode gets its own retry, and kill.

    Episode 1 wedges, is killed by the one allowed retry, and recovers. Episode 2
    is an unrelated, genuinely wedged stage 50 minutes later. It must be killed
    the same way, not escalated while left running.
    """
    clock = FakeClock()
    config = _config(stage_retry_budget=1)
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)

        # --- episode 1: a real wedge, spends the single allowed retry.
        _write_log(clock, "wedged\n", age_s=3600)
        first = watchdog.tick()
        assert _stuck_action(first) == "terminate_group", _stuck_action(first)
        assert watchdog.stage_retries == {"dispatcher": 1}

        # --- the stage recovers and works for a long while.
        _recovered(clock, sup)
        for _ in range(5):
            clock.advance(600)
            _write_log(clock, "healthy\n")
            assert _stuck_action(watchdog.tick()) is None, "a healthy stage must be left alone"

        # --- episode 2: an unrelated, genuinely wedged stage, much later.
        clock.advance(600)
        _write_log(clock, "wedged again\n", age_s=3600)
        wedged_pid = sup.children["dispatcher"].pid
        second = watchdog.tick()

        assert _stuck_action(second) == "terminate_group", (
            f"a fresh stuck episode was not killed; got {_stuck_action(second)!r}. "
            f"stage_retries={watchdog.stage_retries} is a process-lifetime counter, "
            f"so the one allowed retry was spent by an incident 50 minutes ago"
        )
        assert pid_alive(wedged_pid) is False
    finally:
        sup.shutdown()


def test_the_retry_budget_is_spent_by_the_first_incident_only() -> None:
    """The bookkeeping fact, stated directly.

    After a stage has recovered and produced output again, the budget it consumed
    must no longer be counted against it.
    """
    clock = FakeClock()
    config = _config(stage_retry_budget=1)
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)
        _write_log(clock, "wedged\n", age_s=3600)
        watchdog.tick()
        assert watchdog.stage_retries.get("dispatcher", 0) == 1

        _recovered(clock, sup)
        clock.advance(600)
        _write_log(clock, "healthy\n")
        watchdog.tick()

        spent = watchdog.stage_retries.get("dispatcher", 0)
        assert spent == 0, (
            f"stage_retries still reports {spent} retries spent after the stage "
            f"recovered and produced fresh output; the counter is never reset, so "
            f"the budget is per serve process rather than per stuck episode"
        )
    finally:
        sup.shutdown()


def test_a_spent_budget_still_leaves_the_wedged_process_running() -> None:
    """The production consequence: a wedged process is escalated, not killed.

    Once the budget is spent, ``check_stuck_stages`` takes the escalate branch,
    which only alerts. The genuinely wedged stage keeps running — the exact
    failure the docstring's "fail closed" guarantee exists to prevent.
    """
    clock = FakeClock()
    config = _config(stage_retry_budget=1)
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)
        _write_log(clock, "wedged\n", age_s=3600)
        watchdog.tick()
        _recovered(clock, sup)
        clock.advance(3000)
        _write_log(clock, "wedged again\n", age_s=3600)
        wedged_pid = sup.children["dispatcher"].pid
        report = watchdog.tick()

        assert pid_alive(wedged_pid) is True, "precondition: the second wedge is still running"
        assert _stuck_action(report) == "escalate"
        errors = [
            e for e in read_serve_events("op") if e.get("event") == "serve.watchdog.stage_dead"
        ]
        assert errors, "the wedge was reported"
        assert pid_alive(wedged_pid) is True, (
            "a genuinely wedged stage was left running with only an alert: the "
            "retry budget was already consumed by an unrelated incident, so the "
            "kill remediation is permanently disabled for the process lifetime"
        )
    finally:
        sup.shutdown()
