"""Round 2: three defects in the serve supervision chain.

Each test here reproduces a *confirmed* defect by the exact steps in its
report. They are written against the contract the package already states in its
own docstrings, not against an implementation:

``supervisor.py:28-34``
    "Crash-loop detection counts crashes, not exits ... every exit carries a
    ``cause`` and only ``crash`` exits count."

``supervisor.py:29-30``
    "A component restarted three times on purpose — by the no-progress rule, or
    because its command template changed — is healthy."

``supervisor.py:571-575`` (``request_restart``)
    "Tagged ``requested`` so the restart it causes does not consume the crash
    budget ... treating the two the same would stop restarting it and hide the
    real problem behind a crash-loop alert."

``locks.py:23-25``
    "**deadlock** — a ``waiting`` record naming a lock another component
    ``holds``, where that holder is in turn ``waiting`` for a lock this one
    holds. The cycle is explicit in the data, not inferred from timing."

The third test is the reason the second one matters operationally rather than
arithmetically: a false deadlock finding is charged to the same
``max_remediations_per_tick`` budget as a real one, and the deadlock rule runs
*before* the orphan, stuck-stage and no-progress rules, so enough ordinary lock
contention silently starves every real finding in the same tick.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig
from agent_fleet.serve.events import read_serve_events
from agent_fleet.serve.locks import STATE_HELD, STATE_WAITING, LockRegistry
from agent_fleet.serve.paths import component_log_path, pid_path
from agent_fleet.serve.procs import terminate_group
from agent_fleet.serve.serve import ServeLoop
from agent_fleet.serve.supervisor import STATE_CRASH_LOOPING, Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable

#: A child that runs until signalled. Default SIGTERM disposition, so the
#: group TERM the watchdog sends really does kill it and it really does exit
#: with -15.
SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"

#: Generous enough that a loaded box is not what fails these tests.
WAIT_S = 5.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    """Bounded poll. Never blocks past *timeout* — no bare `while True`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _config(command: str, *, crash_threshold: int = 5) -> ServeConfig:
    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=command,
                crash_threshold=crash_threshold,
                crash_window_minutes=15,
                backoff_initial_s=0.0,
                backoff_max_s=0.0,
            )
        },
    )


# --------------------------------------------------------------------- all-1
# An unopenable component log must be a graceful spawn failure, not an
# UnboundLocalError that escapes ServeLoop.run's finally.


def test_an_unopenable_component_log_is_a_graceful_spawn_failure() -> None:
    """``handle`` is bound inside the ``try``; the handler that closes it is too.

    A log that cannot be opened — a directory in its place, or ENOSPC on a full
    volume — raises out of ``log_path.open("ab")`` itself, before ``handle`` is
    ever bound. The ``except`` then touched an unbound local and raised
    ``UnboundLocalError`` instead of returning False after emitting
    ``serve.component.spawn_failed``.
    """
    config = _config(SLEEPER)
    sup = Supervisor("op", config, clock=FakeClock())
    try:
        # A directory where the log file belongs: the parent exists, the open
        # does not.
        log = component_log_path("op", "dispatcher")
        log.mkdir(parents=True, exist_ok=True)

        assert sup.start("dispatcher") is False, (
            "a log that cannot be opened must fail the spawn gracefully, the way an "
            "empty command does"
        )
        failures = [
            e for e in read_serve_events("op") if e["event"] == "serve.component.spawn_failed"
        ]
        assert failures, "the graceful path is the one that reports serve.component.spawn_failed"
        assert failures[-1]["data"]["component"] == "dispatcher"
    finally:
        sup.shutdown()


def test_an_unopenable_component_log_does_not_take_the_serve_loop_down() -> None:
    """End to end: the supervisor must still unwind through ``run``'s finally.

    The blast radius of the UnboundLocalError is not a failed spawn. It escaped
    the ``finally`` in ``ServeLoop.run``, so ``supervisor.shutdown()``,
    ``supervisor.save()`` and ``_clear_pidfile()`` never ran: children were left
    running, state was not persisted, and the pid file was left stale for the
    next serve to try to adopt.
    """
    loop = ServeLoop(
        operator="op",
        config=_config(SLEEPER),
        clock=FakeClock(),
        cgroup_root=Path("/nonexistent-cgroup-root"),
        max_ticks=5,
    )
    log = component_log_path("op", "dispatcher")
    log.mkdir(parents=True, exist_ok=True)

    code = loop.run()

    assert code == 0, f"ServeLoop.run must unwind normally, not raise; got exit code {code}"
    assert not pid_path("op").exists(), (
        "run()'s finally never cleared the pid file, so the next serve tries to adopt "
        "a pid this supervisor no longer owns"
    )


# --------------------------------------------------------------------- all-2
# A watchdog-initiated group TERM is a requested death, not a crash.


def test_a_watchdog_group_term_does_not_consume_the_crash_budget() -> None:
    """Five watchdog TERM/restart cycles must not stop the component.

    The repro is exactly what watchdog rules (a) ``stuck_stage`` and (b)
    ``orphan_blocking`` do: ``terminate_group`` on the component's identity,
    then let the tick reap the exit. ``_reap`` read the -15 as "non-zero, so
    crash", overrode the ``pending_cause`` the caller had recorded, and after
    ``crash_threshold`` cycles the component sat in ``crash_looping`` and was
    never restarted again — despite never having crashed.
    """
    sup = Supervisor("op", _config(SLEEPER, crash_threshold=5), clock=FakeClock())
    try:
        assert sup.start("dispatcher") is True
        for attempt in range(5):
            state = sup.children["dispatcher"]
            identity = state.identity
            assert identity is not None, f"iteration {attempt}: no recorded identity"
            # Tag it the way every watchdog rule does, through the public stop
            # path, then reap the exit with a real tick.
            state.pending_cause = "requested"
            terminate_group(identity, proc_root=sup.proc_root)
            assert _wait_until(
                lambda: (
                    sup._procs.get("dispatcher") is None
                    or sup._procs["dispatcher"].poll() is not None
                ),
                WAIT_S,
            ), f"iteration {attempt}: the group TERM did not land"
            sup.tick()

        state = sup.children["dispatcher"]
        assert state.crash_epochs == [], (
            f"a component the watchdog merely restarted charged {len(state.crash_epochs)} "
            f"crash epochs against the budget (last exit {state.last_exit_code}); only a "
            "component that died on its own may spend it"
        )
        assert state.last_exit_cause == "requested", (
            f"the recorded cause is {state.last_exit_cause!r}, not the 'requested' tag "
            "the caller set before the group TERM"
        )
        assert state.state != STATE_CRASH_LOOPING, (
            f"after {5} watchdog restarts the component was declared crash-looping and "
            "stopped for a fault it never had, hiding the real one behind a "
            "serve.component.crash_loop alert"
        )
        assert state.pid is not None, "the component must be running again"
        assert state.state == "running", f"unexpected state {state.state!r}"
    finally:
        sup.shutdown()


def test_a_child_that_crashes_on_its_own_is_still_a_crash() -> None:
    """The fix must not turn a genuine non-zero exit into a quiet restart.

    The existing budget test only ever exercises the *requested* path, so it
    passes whether or not real crashes still count. This is the other side of
    the same branch: a component that exits 3 by itself is still a crash.

    Two children so the second crash happens after a restart, i.e. after a
    spawn has re-tagged the component ``requested`` — which is precisely the
    state the first child is in too, and what made trusting the tag unsound.
    """
    crasher = f"{sys.executable} -c 'import sys; sys.exit(3)'"
    sup = Supervisor("op", _config(crasher, crash_threshold=2), clock=FakeClock())
    try:
        for expected_crashes in (1, 2):
            assert sup.start("dispatcher") is True
            child = sup._procs["dispatcher"]
            assert _wait_until(lambda c=child: c.poll() is not None, WAIT_S), (
                f"the child never exited (crash {expected_crashes})"
            )
            sup.tick()
            state = sup.children["dispatcher"]
            assert state.last_exit_cause == "crash", (
                f"a self-inflicted non-zero exit was recorded as {state.last_exit_cause!r} "
                f"after {expected_crashes} crashes; serve sent this child no signal, so "
                "falling over is still a crash no matter what tag the spawn left behind"
            )
            assert len(state.crash_epochs) == expected_crashes, (
                f"expected {expected_crashes} crash epochs, got {len(state.crash_epochs)}"
            )
        assert state.state == STATE_CRASH_LOOPING
    finally:
        sup.shutdown()


# --------------------------------------------------------------------- all-3
# An ordinary lock waiter pointing at a genuinely held lock is not a cycle.


def test_ordinary_lock_contention_is_not_a_deadlock(tmp_path: Path) -> None:
    """One waiter on a genuinely held lock is contention, not a cycle.

    The walk breaks as soon as it reaches a record that is not itself waiting,
    but the path it had already built held two records, so it passed the
    ``len(path) < 2`` filter and the contention edge was reported as a cycle.
    """
    registry = LockRegistry("op", proc_root=tmp_path / "proc")
    now = 3_600_000.0
    # The holder is live and working: pid 1, and this process is alive too.
    registry.mark_held("repo-x", holder="merger", pid=os.getpid(), starttime=1, now=now - 3600)
    registry.mark_waiting(
        "lane-7",
        holder="dispatcher",
        pid=os.getpid(),
        starttime=1,
        waiting_for="repo-x",
        wanting="merge PR",
        now=now - 3600,
    )

    cycles = registry.deadlocks(now=now, threshold_minutes=20)

    assert cycles == [], (
        f"a single waiter on a lock that is genuinely held is not a cycle, but "
        f"deadlocks() reported {[r.name for r in cycles[0]]} as one; there is no cycle "
        "to release, so the watchdog would free a lock a working holder still owns"
    )


def test_ordinary_contention_does_not_starve_the_real_findings(tmp_path: Path) -> None:
    """The consequence: false findings eat the whole per-tick budget.

    ``check_deadlocks`` runs before ``check_orphans``/``check_stuck_stages``/
    ``check_no_progress`` and every finding is charged to
    ``max_remediations_per_tick``. Five ordinary contention records — five
    unmerged PRs queued behind one busy repo — consumed the entire budget, so
    the genuinely wedged process in the same tick was deferred, not healed.
    """
    registry = LockRegistry("op", proc_root=tmp_path / "proc")
    now = 3_600_000.0
    registry.mark_held("repo-x", holder="merger", pid=os.getpid(), starttime=1, now=now - 3600)
    for lane in range(5):
        registry.mark_waiting(
            f"lane-{lane}",
            holder="dispatcher",
            pid=os.getpid(),
            starttime=1,
            waiting_for="repo-x",
            wanting="merge PR",
            now=now - 3600,
        )

    assert registry.deadlocks(now=now, threshold_minutes=20) == [], (
        "five queued lanes behind one busy repo is normal contention; none of them is "
        "a cycle, and every false finding is charged to max_remediations_per_tick"
    )
    # The holder's own claim is untouched, because nothing was ever released.
    held = registry.read("repo-x")
    assert held is not None and held.state == STATE_HELD
    assert held.holder == "merger"
    assert registry.read("lane-3") is not None and registry.read("lane-3").state == STATE_WAITING


def test_a_real_two_way_cycle_is_still_detected(tmp_path: Path) -> None:
    """The stricter walk must not stop finding the cycle it exists to find.

    A cycle in this data is *two components each holding a lock the other waits
    for*, which is four records: each lock carries both its ``held`` claim and
    its ``waiting`` edge. One record cannot spell that — a lock that is ``held``
    has no ``waiting_for`` — so the two-way cycle is written as the four nodes
    ``mark_held``/``mark_waiting`` actually produce.
    """
    registry = LockRegistry("op", proc_root=tmp_path / "proc")
    now = 3_600_000.0
    # dispatcher holds repo-x and waits for lane-7 ...
    registry.mark_held("repo-x", holder="dispatcher", pid=os.getpid(), starttime=1, now=now - 3600)
    registry.mark_waiting(
        "dispatch-wait",
        holder="dispatcher",
        pid=os.getpid(),
        starttime=1,
        waiting_for="lane-7",
        wanting="merge PR",
        now=now - 3600,
    )
    # ... while merger holds lane-7 and waits for repo-x.
    registry.mark_held("lane-7", holder="merger", pid=os.getpid(), starttime=1, now=now - 3600)
    registry.mark_waiting(
        "merge-wait",
        holder="merger",
        pid=os.getpid(),
        starttime=1,
        waiting_for="repo-x",
        wanting="start lane",
        now=now - 3600,
    )

    cycles = registry.deadlocks(now=now, threshold_minutes=20)

    assert len(cycles) == 1, (
        f"the real cycle must be found exactly once, not {len(cycles)} times; the two "
        f"waits that open it are two views of one cycle, and reporting it twice charges "
        f"max_remediations_per_tick twice and releases the same claim twice: {cycles}"
    )
    assert {r.name for r in cycles[0]} == {"repo-x", "lane-7", "dispatch-wait"}
