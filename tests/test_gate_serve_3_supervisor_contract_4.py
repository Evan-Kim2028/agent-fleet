"""contract-4: a component that exits 0 on its own must still consume crash budget.

``ChildState.pending_cause`` defaults to ``CAUSE_REQUESTED`` (supervisor.py:149)
and ``Supervisor.start``'s signature defaults to ``cause=CAUSE_REQUESTED`` too,
so a *normal* start leaves ``pending_cause == "requested"``. ``_reap`` (line
449) reads that value and keeps it for a zero exit (line 452), so every
self-exit is recorded as an operator-requested stop and never counted as a
crash. The crash-loop detector is then unreachable for a component that
silently exits, and the component is restarted forever at the backoff ceiling.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.supervisor import (
    CAUSE_CRASH,
    CAUSE_REQUESTED,
    STATE_CRASH_LOOPING,
    ChildState,
    Supervisor,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Exits immediately and cleanly -- the "silently broken" shape.
CLEAN_EXIT = f"{sys.executable} -c 'pass'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _config(*, crash_threshold: int) -> ServeConfig:
    def spec(name: str, command: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=command,
            backoff_initial_s=0.0,
            backoff_max_s=0.0,
            crash_threshold=crash_threshold,
            crash_window_minutes=15,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=0.3,
        components={
            "dispatcher": spec("dispatcher", CLEAN_EXIT),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def test_a_plain_start_leaves_pending_cause_as_requested() -> None:
    """The root cause: ``start()``'s default cause is the *requested* cause."""
    assert ChildState(name="dispatcher").pending_cause == CAUSE_REQUESTED

    sup = Supervisor("op", _config(crash_threshold=99), clock=FakeClock())
    assert sup.start("dispatcher") is True
    assert sup.children["dispatcher"].pending_cause == CAUSE_REQUESTED, (
        "a fresh start must not be tagged 'requested'; only an explicit "
        "stop_component/request_restart should suppress the crash count"
    )


def test_self_exiting_component_is_never_marked_crash_looping() -> None:
    """A component that exits 0 repeatedly is restarted forever, undetected."""
    sup = Supervisor("op", _config(crash_threshold=2), clock=FakeClock())
    try:
        for _ in range(10):
            sup.tick()
            time.sleep(0.05)
            state = sup.children["dispatcher"]
            if state.state == STATE_CRASH_LOOPING:
                break

        state = sup.children["dispatcher"]
        assert state.restarts >= 3, f"expected repeated restarts, got {state.restarts}"
        assert state.crashes_in_window(sup.clock.time(), 900.0) >= 2, (
            "a component that exits on its own, without being asked to, is a "
            f"crash and must consume the budget; recorded crashes="
            f"{len(state.crash_epochs)}, causes={state.last_exit_cause!r}"
        )
        assert sup._crash_looping("dispatcher") is True
        assert state.state == STATE_CRASH_LOOPING
        assert state.crash_epochs, "no crash epoch was ever recorded"
        assert (
            CAUSE_CRASH
            in {
                state.last_exit_cause,
            }
            or state.crash_epochs
        ), "exit cause was mislabelled"
    finally:
        sup.shutdown()


def test_the_mislabel_is_persisted_across_a_supervisor_restart() -> None:
    """The wrong cause is sticky: it is saved and reloaded on the next boot."""
    sup = Supervisor("op", _config(crash_threshold=99), clock=FakeClock())
    try:
        for _ in range(4):
            sup.tick()
            time.sleep(0.05)
        sup.save()
        saved = sup.children["dispatcher"].last_exit_cause
        pending = sup.children["dispatcher"].pending_cause
    finally:
        sup.shutdown()

    fresh = Supervisor("op", _config(crash_threshold=99), clock=FakeClock())
    assert fresh.children["dispatcher"].last_exit_cause == saved
    assert fresh.children["dispatcher"].pending_cause == pending
    assert fresh.children["dispatcher"].crash_epochs == [], (
        "the crash history is empty after several self-exits, so a crash-looping "
        "component is handed a fresh budget on every supervisor restart"
    )
