"""correctness-2: a single stuck subject must not produce an unbounded error stream.

The module docstring's closing rationale for routing is the rule under test:

    The human queue is a file, not a ping. Per-item notification is how a fleet of
    this size becomes unmonitorable — a hundred notifications train a human to mute
    the channel, and then the one that mattered goes unread.

Both escalate branches in the watchdog call ``alert()`` — which is defined in
``serve.events`` as an unconditionally error-level event — on *every* tick, with
no dedupe, no latch, and no state recording that the alert already fired. The
condition that selects the escalate branch (retry budget spent / restart budget
spent) is stable, so the alert re-fires identically every tick forever: 20
``stage_dead`` and 14 ``no_progress`` events in ten ticks, 17 of them at error
level, and counting.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import LEVEL_ERROR, read_serve_events
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.supervisor import Supervisor
from agent_fleet.serve.watchdog import Watchdog

if TYPE_CHECKING:
    from pathlib import Path

SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _config() -> ServeConfig:
    """Both escalate branches armed immediately: budgets of zero.

    This is the state of a component that has already been restarted/retried as
    often as serve is willing to, and which is still wedged. It is the state a
    real fleet sits in for hours at a time.
    """
    return ServeConfig(
        operator="op",
        tick_seconds=15.0,
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=SLEEPER,
                no_progress_restarts=0,
                no_progress_window_minutes=1,
            )
        },
        watchdog=WatchdogConfig(
            stage_timeout_minutes={"lane": 1},
            stage_retry_budget=0,
            no_progress_minutes=1,
            orphan_minutes=10_000,
            stale_lock_minutes=10_000,
            deadlock_minutes=10_000,
        ),
    )


def _alerts(clock: FakeClock, sup: Supervisor, *, ticks: int, tick_seconds: float) -> list[dict]:
    watchdog = Watchdog("op", sup.config, sup, clock=clock)
    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(ticks):
        log.write_text("wedged\n", encoding="utf-8")
        os.utime(log, (clock.time() - 3600, clock.time() - 3600))
        watchdog.tick(queued_depth=3)
        clock.advance(tick_seconds)
    return [
        e
        for e in read_serve_events("op")
        if e.get("event") in ("serve.watchdog.stage_dead", "serve.watchdog.no_progress")
    ]


def test_one_stuck_component_produces_at_most_one_error_alert() -> None:
    """A single stuck subject, alerted once — not once per tick, forever."""
    clock = FakeClock()
    config = _config()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        alerts = _alerts(clock, sup, ticks=10, tick_seconds=15)
        assert alerts, "precondition: the stuck component was reported at least once"
        assert len(alerts) <= 2, (
            f"one wedged component produced {len(alerts)} alerts in 10 ticks "
            f"(reasons {[a.get('data', {}).get('reason', '')[:40] for a in alerts]}); "
            f"the escalate branches call alert() unconditionally with no dedupe, so "
            f"every tick re-notifies the operator about a condition that has not changed"
        )
    finally:
        sup.shutdown()


def test_error_level_alerts_do_not_repeat_for_an_unchanged_condition() -> None:
    """The error stream must stay clear for a genuinely new error.

    Ten identical error-level notifications for one unchanged subject train an
    operator to mute the channel, which is the failure the docstring names.
    """
    clock = FakeClock()
    config = _config()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        alerts = _alerts(clock, sup, ticks=10, tick_seconds=15)
        errors = [a for a in alerts if a.get("level") == LEVEL_ERROR]
        assert len(errors) <= 2, (
            f"{len(errors)} error-level alerts for a single unchanged stuck "
            f"component across 10 ticks; identical reasons: "
            f"{sorted({a.get('data', {}).get('reason', '') for a in errors})}"
        )
    finally:
        sup.shutdown()


def test_alerting_keeps_going_indefinitely_for_the_same_subject() -> None:
    """Ticking further produces the same alert every tick, without end.

    With ``stage_retry_budget=0`` and ``no_progress_restarts=0`` the escalate
    branches are permanently selected, so there is no state that could ever
    change the outcome.
    """
    clock = FakeClock()
    config = _config()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        _alerts(clock, sup, ticks=10, tick_seconds=15)
        first_window = len(
            [
                e
                for e in read_serve_events("op")
                if e.get("event") in ("serve.watchdog.stage_dead", "serve.watchdog.no_progress")
            ]
        )
        # Ten more ticks, nothing about the subject has changed.
        _alerts(clock, sup, ticks=10, tick_seconds=15)
        total = len(
            [
                e
                for e in read_serve_events("op")
                if e.get("event") in ("serve.watchdog.stage_dead", "serve.watchdog.no_progress")
            ]
        )
        assert total - first_window <= 2, (
            f"the same subject produced {total - first_window} further alerts over 10 "
            f"more ticks with nothing changed; a watchdog that re-alerts every tick "
            f"buries any genuinely new error in the operator's error stream"
        )
    finally:
        sup.shutdown()
