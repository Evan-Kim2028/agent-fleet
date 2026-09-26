"""prodsafety-5: the ensure-running liveness check must be fingerprint-gated.

``tick()``'s ensure-running loop (supervisor.py:533) asks only
``pid_alive(...)`` about the recorded pid, with no fingerprint. Every other
decision in this package is fingerprint-gated -- ``adopt()`` refuses a pid whose
start time does not match, and every ``procs.py`` kill goes through
``ProcIdentity.matches``. So after a supervisor restart, a recycled pid that
belongs to an unrelated live process permanently suppresses the component's
start, while status reports the foreign pid as if it were ours.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.procs import starttime_fingerprint
from agent_fleet.serve.supervisor import STATE_RUNNING, ChildState, Supervisor

if TYPE_CHECKING:
    from pathlib import Path

SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _config() -> ServeConfig:
    def spec(name: str, command: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=command,
            backoff_initial_s=0.0,
            backoff_max_s=0.0,
            crash_threshold=3,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=0.3,
        components={
            "dispatcher": spec("dispatcher", SLEEPER),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def test_a_recycled_pid_does_not_suppress_the_component_start() -> None:
    sup = Supervisor("op", _config(), clock=FakeClock())
    try:
        assert sup.start("dispatcher") is True
        sup.save()
    finally:
        sup.shutdown()

    # Simulate pid recycling: a genuinely live but unrelated process is recorded
    # against our component, with a fingerprint that does not match it.
    stranger = os.getpid()
    real_fingerprint = starttime_fingerprint(stranger)
    assert real_fingerprint is not None, "could not fingerprint the stranger pid"

    stale = sup.children["dispatcher"]
    stale.state = STATE_RUNNING
    stale.pid = stranger
    stale.starttime = 999999999
    stale.adopted = False
    sup.save()

    fresh = Supervisor("op", _config(), clock=FakeClock())
    try:
        restored = fresh.children["dispatcher"]
        assert restored.pid == stranger
        assert restored.starttime == 999999999
        assert fresh.adopt("dispatcher") is False, (
            "precondition: adopt() correctly refuses the recycled pid"
        )

        fresh.tick()

        assert "dispatcher" in fresh._procs, (
            "the component was never started: tick()'s ensure-running loop treats "
            f"the recycled pid {stranger} as proof that the component is alive, "
            "so the dispatcher stays dead while status reports a foreign pid"
        )
        assert fresh._procs["dispatcher"].pid != stranger
    finally:
        fresh.shutdown()


def test_ensure_running_check_is_fingerprint_gated_for_adopted_children() -> None:
    """The liveness probe must not accept a pid whose fingerprint disagrees."""
    sup = Supervisor("op", _config(), clock=FakeClock())
    try:
        stranger = os.getpid()
        state = sup.children.setdefault("dispatcher", ChildState(name="dispatcher"))
        state.state = STATE_RUNNING
        state.pid = stranger
        state.starttime = 999999999

        sup.tick()

        started = sup._procs.get("dispatcher")
        assert started is not None, (
            "no process was spawned for 'dispatcher' even though the recorded pid "
            "is an unrelated process; the supervisor is now permanently supervising "
            "a stranger and never starting its own component"
        )
        assert started.pid != stranger
        assert starttime_fingerprint(started.pid) != 999999999
    finally:
        sup.shutdown()
