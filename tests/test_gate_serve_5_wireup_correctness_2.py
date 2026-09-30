"""correctness-2: a SIGKILLed component is a crash, not a requested stop.

``supervisor._KILLED_BY_SIGNAL`` is ``{-SIGKILL, -SIGINT}`` and
``_REQUESTED_CAUSES`` is ``{CAUSE_REQUESTED, CAUSE_REQUESTED_KILLED}``. In
``_reap``, a child whose recorded cause is already a *requested* one and whose
exit code is in ``_KILLED_BY_SIGNAL`` is booked as ``CAUSE_REQUESTED_KILLED``,
which is a requested cause and is therefore **never** appended to
``state.crash_epochs``.

But ``Supervisor.start`` defaults ``cause=CAUSE_REQUESTED``, and ``tick``'s
deadline loop re-spawns with ``cause=state.last_exit_cause`` — so a component
that is SIGKILLed by the OOM killer on *every* start is booked as "asked to
stop" forever. Its crash budget stays at zero, it never reaches
``crash_looping``, and it restarts indefinitely with no ``crash_loop`` alert.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig
from agent_fleet.serve.supervisor import STATE_CRASH_LOOPING, Supervisor

if TYPE_CHECKING:
    from pathlib import Path

#: Kills itself with SIGKILL immediately: the OOM-killer shape.
SIGKILL_SELF = f"{sys.executable} -c 'import os,signal; os.kill(os.getpid(), signal.SIGKILL)'"

#: A control that dies the same way but is *booked* as a crash, so the
#: difference between the two is the classification and nothing else.
EXIT_ONE = f"{sys.executable} -c 'import sys; sys.exit(1)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _config(command: str) -> ServeConfig:
    return ServeConfig(
        operator="op",
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=command,
                crash_threshold=3,
                crash_window_minutes=15,
                backoff_initial_s=0.0,
                backoff_max_s=0.0,
                shutdown_grace_s=0.2,
            )
        },
        shutdown_grace_s=0.2,
    )


def _drive(command: str, ticks: int = 9) -> Supervisor:
    clock = FakeClock()
    sup = Supervisor("op", _config(command), clock=clock)
    sup.defer_restarts = True
    for _ in range(ticks):
        time.sleep(0.25)  # let the child really die and be reapable
        clock.advance(600)
        sup.tick()
    return sup


def test_a_sigkill_on_every_start_eventually_trips_the_crash_budget() -> None:
    """Three SIGKILL deaths inside the window must mark the component."""
    sup = _drive(SIGKILL_SELF)
    try:
        state = sup.children["dispatcher"]
        assert len(state.crash_epochs) >= 3, (
            f"SIGKILL deaths were not charged to the crash budget: "
            f"crash_epochs={state.crash_epochs} after {state.restarts} restarts "
            f"(last cause {state.last_exit_cause!r}, code {state.last_exit_code})"
        )
    finally:
        sup.shutdown()


def test_a_sigkill_looping_component_is_marked_crash_looping() -> None:
    """The budget exists so this stops; without it, serve restarts forever."""
    sup = _drive(SIGKILL_SELF, ticks=12)
    try:
        state = sup.children["dispatcher"]
        assert state.state == STATE_CRASH_LOOPING, (
            f"state is {state.state!r} after {state.restarts} restarts and "
            f"{len(state.crash_epochs)} recorded crashes; a component killed by "
            "SIGKILL on every start is not asked to stop by anyone"
        )
    finally:
        sup.shutdown()


def test_a_sigkill_death_is_not_booked_as_a_requested_cause() -> None:
    """A signal serve never sends is evidence the child died on its own.

    ``_SIGNALLED_BY_SERVE`` is exactly ``{-SIGTERM}``. SIGKILL is not in it, so
    a -9 exit can only be booked as a crash.
    """
    sup = _drive(SIGKILL_SELF, ticks=4)
    try:
        state = sup.children["dispatcher"]
        assert state.last_exit_code == -9, f"expected a SIGKILL exit, got {state.last_exit_code}"
        assert state.last_exit_cause == "crash", (
            f"SIGKILL booked as {state.last_exit_cause!r}; a kill serve never sent "
            "is not a stop anyone requested"
        )
    finally:
        sup.shutdown()


def test_the_control_crash_path_also_trips_the_budget() -> None:
    """A plain non-zero exit charges the budget — so the test is discriminating.

    If this passes while the SIGKILL case does not, the budget machinery is
    sound and the SIGKILL classification is the specific defect.
    """
    sup = _drive(EXIT_ONE, ticks=6)
    try:
        state = sup.children["dispatcher"]
        assert len(state.crash_epochs) >= 3, (
            f"a plain crash should charge the budget; crash_epochs={state.crash_epochs}"
        )
    finally:
        sup.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
