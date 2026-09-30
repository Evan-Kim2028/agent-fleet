"""correctness-3: `serve watchdog --apply` must not race a live supervisor.

``cmd_serve_watchdog`` builds its *own* ``Supervisor`` over the same serve
directory as the running one:

    supervisor = _supervisor_for(operator, config)   # cli.py, in-process
    watchdog   = Watchdog(operator, config, supervisor, ...)

The running supervisor's pid file is the only thing linking the two, and
``Supervisor.start`` calls ``adopt(name)`` first — so the watchdog-side
supervisor *adopts* the live component rather than recognising that the role is
already owned by a live supervisor process. ``no_progress`` then calls
``request_restart``, which stops and immediately respawns the role from inside
the short-lived watchdog process, while the real supervisor reaps its child and
respawns it too. Two live processes for one role, neither visible to the crash
budget, both holding the same capacity file.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.supervisor import Supervisor

if TYPE_CHECKING:
    from pathlib import Path

CONFIG = ServeConfig(
    operator="op",
    components={
        "dispatcher": ComponentSpec(name="dispatcher", command="sleep 3600", shutdown_grace_s=0.3)
    },
    shutdown_grace_s=0.3,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def test_a_second_supervisor_must_not_spawn_onto_an_owned_role() -> None:
    """The watchdog-side supervisor may not produce a second live dispatcher."""
    real = Supervisor("op", CONFIG, clock=SystemClock())
    watchdog_side = Supervisor("op", CONFIG, clock=SystemClock())
    try:
        real.start("dispatcher")
        owned_pid = real.children["dispatcher"].pid
        time.sleep(0.3)
        assert pid_alive(owned_pid), "the real supervisor's dispatcher should be running"

        # Exactly what `serve watchdog --apply` does when rule (e) fires.
        acted = watchdog_side.request_restart("dispatcher", reason="no_progress")
        rival = watchdog_side.children["dispatcher"].pid

        assert not acted or rival == owned_pid, (
            f"the watchdog-side supervisor spawned a rival dispatcher {rival} for a "
            f"role already owned by the live supervisor's pid {owned_pid}"
        )
        assert not (pid_alive(owned_pid) and pid_alive(rival) and rival != owned_pid), (
            f"two live processes for one role: {owned_pid} and {rival}"
        )
    finally:
        watchdog_side.shutdown()
        real.shutdown()


def test_a_supervisor_must_refuse_to_act_on_a_role_owned_by_a_live_supervisor() -> None:
    """The supervisor lock is the ownership signal; it must be consulted."""
    from agent_fleet.serve.paths import ensure_serve_dir, lock_path

    real = Supervisor("op", CONFIG, clock=SystemClock())
    try:
        real.start("dispatcher")
        owned = real.children["dispatcher"].pid

        # While the real supervisor is alive, it holds serve/serve/<op>/serve.lock.
        ensure_serve_dir("op")
        with lock_path("op").open("rb"):
            pass

        # A second supervisor constructed over the same dir must notice the role
        # is already supervised and decline to respawn it.
        second = Supervisor("op", CONFIG, clock=SystemClock())
        try:
            second.request_restart("dispatcher", reason="no_progress")
            spawned = second.children["dispatcher"].pid
            assert spawned == owned, (
                f"second supervisor replaced the owned dispatcher {owned} with {spawned}"
            )
        finally:
            second._procs.clear()
            second._handles.clear()
    finally:
        real.shutdown()


def test_request_restart_does_not_double_start_while_the_owner_runs() -> None:
    """Any `request_restart` for an owned role must leave one live process."""
    real = Supervisor("op", CONFIG, clock=SystemClock())
    watchdog_side = Supervisor("op", CONFIG, clock=SystemClock())
    try:
        real.start("dispatcher")
        owned = real.children["dispatcher"].pid
        time.sleep(0.3)

        for _ in range(3):
            watchdog_side.request_restart("dispatcher", reason="no_progress")
            time.sleep(0.2)
            rival = watchdog_side.children["dispatcher"].pid
            assert not (pid_alive(owned) and pid_alive(rival) and rival != owned), (
                f"rival dispatcher {rival} alongside the owned {owned}"
            )
    finally:
        watchdog_side.shutdown()
        real.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
