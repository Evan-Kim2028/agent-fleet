"""spec-1: a component that is demonstrably producing output is not wedged.

Rule (e) ``no_progress`` is documented as "a component has queued work and has
emitted nothing for the window". The implementation compares
``now - ChildState.last_event_epoch``, but that field is written only by
``Supervisor.start`` and ``Supervisor.adopt`` — a grep of the tree finds no
other writer, and in particular nothing that observes the component's own
activity. So the detector that is supposed to notice a busy component cannot,
and it terminates and respawns one every tick while its log is still filling.

The component here writes continuously to the very log the watchdog treats as a
component's tracked output, so "is it working?" has an objective answer at
assert time — and the rule must agree.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.supervisor import Supervisor
from agent_fleet.serve.watchdog import RULE_NO_PROGRESS, Watchdog

if TYPE_CHECKING:
    from pathlib import Path

#: A component that appends to its own tracked log forever. The path is passed
#: as argv[1] so the template expands to the real log rather than a guess.
BUSY = (
    sys.executable + ' -c "import sys,time\n'
    "p = sys.argv[1]\n"
    "for _ in range(100000):\n"
    "    open(p, 'a').write('work\\n')\n"
    '    time.sleep(0.02)" '
    "{serve_dir}/components/dispatcher.log"
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _config() -> ServeConfig:
    return ServeConfig(
        operator="op",
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=BUSY,
                no_progress_restarts=2,
                no_progress_window_minutes=30,
            )
        },
        watchdog=WatchdogConfig(
            no_progress_minutes=1,
            max_remediations_per_tick=5,
            # Not a stuck stage: this is only about rule (e).
            stage_timeout_minutes={"lane": 100000},
        ),
    )


def _wait_for_growth(log: Path, *, baseline: int, timeout: float) -> bool:
    """Bounded poll until *log* grows past *baseline*. Never blocks past *timeout*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        size = log.stat().st_size if log.exists() else 0
        if size > baseline:
            return True
        time.sleep(0.05)
    return False


def test_a_component_whose_log_is_growing_is_not_terminated() -> None:
    """The claim's core: output on every tick, remediation on every tick."""
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    try:
        sup.start("dispatcher")
        watchdog = Watchdog("op", _config(), sup, clock=clock)
        log = component_log_path("op", "dispatcher")

        # Let the component get going before the first measurement, so a tick is
        # never judged on interpreter start-up rather than on its own behaviour.
        assert _wait_for_growth(log, baseline=0, timeout=30.0), (
            "the busy component produced no output at all; this test cannot "
            "distinguish a working component from a dead one"
        )

        terminations = []
        for _ in range(3):
            before = log.stat().st_size if log.exists() else 0
            assert _wait_for_growth(log, baseline=before, timeout=30.0), (
                "the component should be producing output continuously"
            )
            clock.advance(3600)
            report = watchdog.tick(queued_depth=5)
            terminations.extend(r for r in report.remediations if r.rule == RULE_NO_PROGRESS)

        assert terminations == [], (
            "a component whose log grew on every tick was acted on: "
            f"{[r.action for r in terminations]}"
        )
    finally:
        sup.shutdown()


def test_the_pid_does_not_change_while_the_component_is_working() -> None:
    """A working component must keep its process across watchdog ticks."""
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    try:
        sup.start("dispatcher")
        watchdog = Watchdog("op", _config(), sup, clock=clock)

        first = sup.children["dispatcher"].pid
        pids = [first]
        for _ in range(3):
            time.sleep(0.5)
            clock.advance(3600)
            watchdog.tick(queued_depth=5)
            pids.append(sup.children["dispatcher"].pid)

        assert len(set(pids)) == 1, f"the busy dispatcher was killed and respawned: {pids}"
    finally:
        sup.shutdown()


def test_the_restart_budget_never_engages_for_a_working_component() -> None:
    """The claim's second half: churn must not run the no_progress budget dry."""
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    try:
        sup.start("dispatcher")
        watchdog = Watchdog("op", _config(), sup, clock=clock)
        for _ in range(3):
            time.sleep(0.4)
            clock.advance(3600)
            watchdog.tick(queued_depth=5)

        state = sup.children["dispatcher"]
        assert state.no_progress_restarts == [], (
            f"a working component spent its restart budget: {state.no_progress_restarts}"
        )
    finally:
        sup.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
