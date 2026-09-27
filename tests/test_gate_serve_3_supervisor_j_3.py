"""j_3: a component that cannot even spawn must back off and trip the crash budget.

One backoff schedule in this module, and the spawn-failure path never touches
it. ``backoff_for()`` and the ``crash_looping`` verdict are only ever reached
from the ``_reap()`` branch of :meth:`Supervisor.tick`, and a command that
raises ``OSError`` on spawn never produces a process to reap. The state is left
at ``stopped``, and the ensure-running loop in ``tick()`` skips only
``crash_looping`` — so it calls ``start()`` again on the very next tick. Against
a 15-second supervisor cadence that is one doomed spawn attempt per tick
forever, and the operator is told nothing: no crash-loop state, no alert, and
``status_rows()`` reporting a component that is simply "stopped".

The assertions are on the contract a fix has to honour — stop retrying, and
surface the failure — rather than on the mechanism. Gating the retry on elapsed
time and sleeping inline both satisfy them, so the test does not insist on
``clock.sleep`` having been called.

No real process is spawned: ``/nonexistent/...`` fails deterministically inside
``Popen``, which is the ordinary shape of a bad path, a moved wrapper, or a
missing interpreter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import read_serve_events
from agent_fleet.serve.supervisor import STATE_CRASH_LOOPING, Supervisor

if TYPE_CHECKING:
    from pathlib import Path

#: A command that can never be executed. ``Popen`` raises ``FileNotFoundError``.
MISSING_BINARY = "/nonexistent/binary-does-not-exist"

#: Supervisor cadence the claim is written against, in ticks.
TICKS = 200

#: The crash budget the operator configured.
CRASH_THRESHOLD = 3


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _config() -> ServeConfig:
    def spec(name: str, cmd: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=cmd,
            backoff_initial_s=5.0,
            backoff_max_s=300.0,
            crash_threshold=CRASH_THRESHOLD,
            crash_window_minutes=15,
            no_progress_restarts=2,
            no_progress_window_minutes=30,
            shutdown_grace_s=1.0,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=15.0,
        shutdown_grace_s=1.0,
        components={
            "dispatcher": spec("dispatcher", MISSING_BINARY),
            "merger": spec("merger", None),
            "janitor": spec("janitor", None),
        },
        watchdog=WatchdogConfig(),
    )


def _events(name: str) -> list[dict[str, object]]:
    return [row for row in read_serve_events("op") if row.get("event") == name]


def test_unspawnable_component_stops_retrying_and_is_reported_as_crash_looping() -> None:
    """A persistently failing spawn must consume the crash budget, not every tick."""
    sup = Supervisor("op", _config(), clock=FakeClock())

    for _ in range(TICKS):
        sup.tick()

    state = sup.children["dispatcher"]
    spawn_failures = _events("serve.component.spawn_failed")

    assert len(spawn_failures) <= CRASH_THRESHOLD * 2, (
        f"{len(spawn_failures)} spawn attempts for a binary that does not exist across "
        f"{TICKS} ticks (one per tick, with backoff_initial_s=5s never applied): "
        "the spawn-failure branch records a crash but leaves the state at 'stopped', "
        "so the ensure-running loop, which skips only 'crash_looping', retries "
        "immediately every time"
    )

    assert state.state == STATE_CRASH_LOOPING, (
        f"after {TICKS} failed spawns against a crash_threshold of {CRASH_THRESHOLD} the "
        f"component reports {state.state!r}: the spawn-failure path never reaches the "
        "reap path where the crash budget is judged, so the supervisor retries a "
        "component that can never start and never tells the operator it is broken"
    )

    assert _events("serve.component.crash_loop"), (
        "a component that cannot spawn at all never emitted the crash-loop alert; "
        "the operator gets silence while the supervisor retries it every tick"
    )

    row = next(r for r in sup.status_rows() if r["component"] == "dispatcher")
    assert row["state"] == STATE_CRASH_LOOPING, (
        f"`serve status` reports the broken component as {row['state']!r}, so the "
        "only place an operator would notice is silent about it"
    )
