"""contract-2: the no-progress burst budget must not be measured backwards from the
last restart.

The escalation guard is::

    recent = state.no_progress_restarts
    restarts = sum(1 for e in recent if recent[-1] - e <= budget_window_s)
    if restarts >= spec.no_progress_restarts:  -> escalate, else restart

Because ``recent`` grows by exactly one entry per fired restart, that sum is
identically ``len(recent) - 1``: every restart but the newest, regardless of age.
So the count can never exceed 1, a budget of 2 is unreachable, and the escalation
branch the docstring promises ("a component restarted for this rule twice inside
its window is a component that needs a human, so the third finding escalates
instead of looping") is dead code whenever consecutive restarts land more than
one window apart — which is the normal cadence, because each restart resets the
rule's own idle timer.

The window is also taken as ``no_progress_window_minutes``, which the shipped
config sets equal to ``no_progress_minutes`` — the exact interval at which the
rule itself fires. Whether the fleet escalates or churns a wedged component
forever therefore reduces to tick alignment (a 61s vs 70s tick, both at the
shipped ratios, produces restart-forever vs escalate).

These tests drive the real expression on the real recorded restart epochs; the
restart history is produced by the rule itself so the arithmetic under test is
the shipped one.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
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
    """Scaled-down defaults preserving the defect's structure: the budget window
    equals the no-progress window and the restart budget is 2, both as shipped."""
    return ServeConfig(
        operator="op",
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=SLEEPER,
                no_progress_restarts=2,
                no_progress_window_minutes=1,
            )
        },
        watchdog=WatchdogConfig(no_progress_minutes=1),
    )


def _burst_count(recent: list[float], budget_window_s: float) -> int:
    """The exact expression from ``check_no_progress``."""
    return sum(1 for e in recent if recent[-1] - e <= budget_window_s) if recent else 0


def _restart_history(*, tick_seconds: float, ticks: int = 5) -> list[float]:
    """Let the real rule build the restart history for *tick_seconds* ticks."""
    clock = FakeClock()
    config = _config()
    sup = Supervisor("op", config, clock=clock)
    sup.start("dispatcher")
    try:
        watchdog = Watchdog("op", config, sup, clock=clock)
        log = component_log_path("op", "dispatcher")
        log.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(ticks):
            clock.advance(tick_seconds)
            log.write_text("wedged\n", encoding="utf-8")
            os.utime(log, (clock.time(), clock.time()))
            watchdog.tick(queued_depth=3)
        return list(sup.children["dispatcher"].no_progress_restarts)
    finally:
        sup.shutdown()


def test_the_budget_window_equals_the_rule_fire_interval() -> None:
    """Config fact underpinning the claim: the burst window is not independent
    of the interval at which the rule fires, so the two are always entangled."""
    assert WatchdogConfig().no_progress_minutes == 30
    assert ComponentSpec(name="dispatcher").no_progress_window_minutes == 30


def test_a_burst_of_restarts_older_than_the_window_still_counts_as_one() -> None:
    """The one-line defect, in isolation.

    Given the restart history the rule itself records at a 61s tick — each
    restart 61s apart, the budget window is 60s — a component restarted
    repeatedly must eventually reach its budget of 2 so the third finding can
    escalate. Measured backwards from the newest restart it never does.
    """
    config = _config()
    budget_window_s = float(config.component("dispatcher").no_progress_window_minutes) * 60

    recent = _restart_history(tick_seconds=61)
    assert len(recent) >= 3, f"expected the rule to record several restarts, got {recent}"
    # Consecutive restarts are genuinely outside the window with respect to the
    # clock; only the backwards-from-last measurement fails to see that.
    assert recent[-1] - recent[0] > budget_window_s

    measured = _burst_count(recent, budget_window_s)
    assert measured >= 2, (
        f"{len(recent)} restarts spanning {recent[-1] - recent[0]:.0f}s were counted as a "
        f"burst of {measured} against a budget of 2, because the window is measured "
        f"backwards from the newest restart rather than from the clock; the escalation "
        f"branch is unreachable and the component is restarted forever"
    )
