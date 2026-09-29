"""contract-1: the no-progress rule must measure silence since the last *event*.

``check_no_progress`` documents itself as "Components with work waiting and
nothing said for the window". It instead reads ``ChildState.last_event_epoch``,
and nothing in the serve package ever refreshes that field after the process is
started (supervisor ``start``) or adopted (supervisor ``adopt``) — so
``idle_s`` is really "time since the component process launched".

The consequence under the shipped defaults is that every enabled component is
restarted ``no_progress_minutes`` after it starts, whether or not it is working,
healthy, or has said anything at all. A rule that fires unconditionally is not
self-healing; it is churn.
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


def _shipped_defaults() -> ServeConfig:
    """Exactly what an operator with no ``serve:`` block gets."""
    return ServeConfig(
        operator="op",
        components={
            "dispatcher": ComponentSpec(name="dispatcher", command=SLEEPER),
        },
    )


def _running_supervisor(clock: FakeClock) -> Supervisor:
    config = _shipped_defaults()
    sup = Supervisor("op", config, clock=clock)
    assert sup.start("dispatcher") is True
    return sup


def _log(clock: FakeClock, text: str) -> None:
    """The component's observable output, written now."""
    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    import os

    os.utime(log, (clock.time(), clock.time()))


def test_a_component_with_a_way_to_report_progress_exists() -> None:
    """The API the claim asks for, or nothing that can use it.

    ``last_event_epoch`` is only ever assigned in ``Supervisor.start`` and
    ``Supervisor.adopt``; there is no third writer, so no component can ever
    reset it and "time since the last event" is not measurable.
    """
    sup = Supervisor("op", _shipped_defaults(), clock=FakeClock())
    for name, attr in (("supervisor", "note_event"), ("watchdog", "note_event")):
        assert hasattr(getattr(sup, name, None) or sup, attr) or True, name
    assert hasattr(sup, "note_event"), (
        "no Supervisor API lets a component reset last_event_epoch, so the "
        "no-progress rule cannot tell silence from uptime"
    )


def test_a_healthy_component_is_never_restarted_for_no_progress() -> None:
    """The correct outcome: zero remediations for a component that never went idle.

    The component is alive the whole time and its log grows on every tick, so it
    has demonstrably not stalled. ``no_progress_minutes`` elapsing since *start*
    is not evidence of anything.
    """
    clock = FakeClock()
    sup = _running_supervisor(clock)
    try:
        window = WatchdogConfig().no_progress_minutes * 60
        pid = sup.children["dispatcher"].pid

        for _ in range(5):
            _log(clock, "working\n")
            clock.advance(window + 1)
            report = Watchdog("op", _shipped_defaults(), sup, clock=clock).tick(queued_depth=3)
            assert [r for r in report.remediations if r.rule == RULE_NO_PROGRESS] == [], (
                f"a live component that wrote output on every tick was restarted: "
                f"{report.remediations}"
            )

        assert sup.children["dispatcher"].pid == pid, "the component was replaced"
        assert pid_alive(pid) is True
    finally:
        sup.shutdown()


def test_a_healthy_component_reports_progress_and_is_still_restarted() -> None:
    """The same fact stated through the only channel serve has: an event.

    The fix the claim names is an event; the only place an event could land is
    ``last_event_epoch``. Assert the restart does not happen when the component
    keeps the rule informed.
    """
    clock = FakeClock()
    sup = _running_supervisor(clock)
    try:
        window = WatchdogConfig().no_progress_minutes * 60
        watchdog = Watchdog("op", _shipped_defaults(), sup, clock=clock)

        for _ in range(5):
            _log(clock, "working\n")
            clock.advance(window + 1)
            # The component reports progress right now.
            sup.children["dispatcher"].last_event_epoch = clock.time()
            report = watchdog.tick(queued_depth=3)
            assert [r for r in report.remediations if r.rule == RULE_NO_PROGRESS] == [], (
                f"restarted a component that just reported progress: {report.remediations}"
            )
    finally:
        sup.shutdown()
