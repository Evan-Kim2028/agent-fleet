"""Rule (a) stuck_stage must actually end the process, not just report it.

The module docstring's load-bearing promise is "**Fail closed, then let the owner
retry.** A stuck stage is *killed* and marked dead rather than left running."
Every other rule's remediation is a record write or a signal, so "killed" is
the one word in that sentence that has to be load-bearing. Rule (a)'s
remediation is ``terminate_group`` (:func:`agent_fleet.serve.procs.terminate_group`),
which by its own docstring is **TERM only**; the other half of the kill lives in
``escalate_kill_group``, which is reached only through
:meth:`Watchdog.escalate_pending_groups`.

A component that ignores SIGTERM is the ordinary case for a wedged agent — an
agent process with a handler installed, a wrapper that traps signals, anything
that is slow to unwind. The tests below use a real process that really does
``signal.signal(signal.SIGTERM, signal.SIG_IGN)``, really spawned by the real
supervisor, so every assertion is about ``/proc`` and not about a mock. The
child prints ``READY`` only *after* installing the handler, so the test cannot
race the interpreter's startup and accidentally kill a process that was not yet
stubborn.

``WatchdogConfig.kill_grace_s`` is documented as "seconds of grace to wait
before escalating TERM to KILL", and the watchdog docstring says the grace is
"spent by the *caller's* next tick". No code inside the package spends it:
``check_stuck_stages`` records the victim nowhere, and ``tick`` reaches the
escalation only through a ``pending_kills`` argument that nothing ever supplies.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import events_path
from agent_fleet.serve.paths import component_log_path
from agent_fleet.serve.procs import ProcIdentity, pid_alive
from agent_fleet.serve.supervisor import Supervisor
from agent_fleet.serve.watchdog import RULE_STUCK_STAGE, Watchdog

#: A component that survives SIGTERM by design. This is the whole point: a
#: remediation that only ever sends TERM can never make this process die, and an
#: ordinary ``time.sleep`` sleeper would hide the defect by dying on the first
#: signal. READY is printed only after the handler is installed, so a TERM that
#: lands during interpreter startup cannot be mistaken for a real survivor.
STUBBORN = (
    f"{sys.executable} -c "
    "\"import signal, sys, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('SIGTERM-IGN-READY', flush=True); time.sleep(300)\""
)

#: Long enough to be unambiguous, short enough to keep the test quick.
KILL_GRACE_S = 0.5

#: TERM on the first tick, then every later tick. The watchdog's own budget
#: allows one terminate plus a final escalate, so four is well past the point at
#: which a correct watchdog must have delivered SIGKILL.
TICKS = 4


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def _config() -> ServeConfig:
    """Rule (a) alone, with every other rule pushed out of the way.

    The stuck stage is the only finding these tests want, so the other four
    thresholds are set beyond the clock's position rather than "disabled" — the
    detectors read real config, and a 100000-minute window cannot fire against a
    clock parked at ~1000.
    """
    return ServeConfig(
        operator="op",
        components={"dispatcher": ComponentSpec(name="dispatcher", command=STUBBORN)},
        watchdog=WatchdogConfig(
            stage_timeout_minutes={"lane": 1},
            stage_retry_budget=1,
            kill_grace_s=KILL_GRACE_S,
            max_remediations_per_tick=5,
            no_progress_minutes=100_000,
            orphan_minutes=100_000,
            stale_lock_minutes=100_000,
            deadlock_minutes=100_000,
        ),
    )


def _wait_for_stubborn_handler() -> None:
    """Block until the child has really installed its SIGTERM handler.

    Without this the watchdog can win a race it should lose: a SIGTERM delivered
    while the interpreter is still booting hits the default disposition and kills
    the child, which would make the test pass for the wrong reason.
    """
    log = component_log_path("op", "dispatcher")
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if log.exists() and "SIGTERM-IGN-READY" in log.read_text(
            encoding="utf-8", errors="replace"
        ):
            return
        time.sleep(0.02)
    pytest.fail(f"the stubborn component never reported readiness; log: {log}")


def _backdate_log(clock: FakeClock) -> None:
    """The component's log, an hour stale, so the stage reads as wedged."""
    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("stuck\n")
    old = clock.time() - 3600
    os.utime(log, (old, old))


def _killed_by_group() -> bool:
    """Did the watchdog actually escalate a *group* to SIGKILL?

    Read from the serve-local event mirror rather than from anything the test
    records itself, so a pass cannot come from the test's own bookkeeping.
    """
    path = events_path("op")
    if not path.exists():
        return False
    return "serve.watchdog.kill_group_escalated" in path.read_text(
        encoding="utf-8", errors="replace"
    )


def _settle(seconds: float) -> None:
    """Bounded wait for a signal to land, instead of an unbounded sleep."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.02)


def _cleanup(sup: Supervisor, identity: ProcIdentity | None) -> None:
    """Take down the one process these tests created, by recorded pid only.

    SIGKILL goes to the exact pid this test's supervisor spawned, checked
    against its start-time fingerprint — never by name or pattern, because this
    box runs many other agents.
    """
    if identity is not None and identity.matches():
        with contextlib.suppress(OSError):
            os.kill(identity.pid, signal.SIGKILL)
        _settle(1.0)
    with contextlib.suppress(Exception):
        sup.shutdown()


def _tick_until_stuck_stage_wedges(
    sup: Supervisor, watchdog: Watchdog, identity: ProcIdentity, clock: FakeClock
) -> list[str]:
    """Tick the watchdog the way serve drives it, returning the actions taken.

    Every tick spends ``kill_grace_s`` of both clocks before the next one, which
    is exactly what the watchdog docstring says happens between a TERM and its
    KILL ("the grace is spent by the *caller's* next tick").
    """
    actions: list[str] = []
    for _ in range(TICKS):
        report = watchdog.tick()
        stuck = [r for r in report.remediations if r.rule == RULE_STUCK_STAGE]
        actions.append(stuck[0].action if stuck else "-")
        clock.advance(KILL_GRACE_S)
        _settle(KILL_GRACE_S)
    return actions


def test_a_sigterm_ignoring_stuck_component_is_killed_by_the_watchdog() -> None:
    """The docstring's promise: a stuck stage is *killed*, not left running.

    Rule (a) has exactly one remediation, ``terminate_group``, and that function
    is documented "TERM only". A component that ignores SIGTERM therefore
    survives every tick — while the watchdog records ``signalled=True``, a
    successful termination it never performed, on every tick, for as long as the
    fleet runs.
    """
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    identity: ProcIdentity | None = None
    try:
        assert sup.start("dispatcher") is True
        identity = sup.children["dispatcher"].identity
        assert identity is not None and identity.starttime is not None
        _wait_for_stubborn_handler()
        assert pid_alive(identity.pid) is True, "precondition: the component is running"

        _backdate_log(clock)
        watchdog = Watchdog("op", _config(), sup, clock=clock)
        actions = _tick_until_stuck_stage_wedges(sup, watchdog, identity, clock)

        # The claim under test: after the documented grace, the process is gone.
        assert pid_alive(identity.pid) is False, (
            f"a component that ignores SIGTERM survived {TICKS} watchdog ticks "
            f"({TICKS * KILL_GRACE_S:.1f}s of configured kill_grace_s). "
            f"stuck_stage actions: {actions}. The watchdog reported a termination "
            f"it never performed, and the module docstring's 'a stuck stage is "
            f"killed and marked dead rather than left running' does not hold."
        )
    finally:
        _cleanup(sup, identity)


def test_stuck_stage_escalates_to_sigkill_through_the_group_kill() -> None:
    """The other half of the kill is a group kill, and nothing ever calls it.

    ``terminate_group`` signals the whole group because serve spawned the child
    with ``start_new_session=True``; the matching escalation is
    ``escalate_kill_group``, reachable only through
    ``Watchdog.escalate_pending_groups``. That method has no caller anywhere in
    the package, so the group half of rule (a)'s kill is unreachable in
    production and only the TERM half ever runs.
    """
    clock = FakeClock()
    sup = Supervisor("op", _config(), clock=clock)
    identity: ProcIdentity | None = None
    try:
        assert sup.start("dispatcher") is True
        identity = sup.children["dispatcher"].identity
        assert identity is not None and identity.starttime is not None
        _wait_for_stubborn_handler()

        _backdate_log(clock)
        watchdog = Watchdog("op", _config(), sup, clock=clock)
        actions = _tick_until_stuck_stage_wedges(sup, watchdog, identity, clock)

        assert _killed_by_group(), (
            f"stuck_stage actions were {actions} and no "
            f"serve.watchdog.kill_group_escalated event was ever emitted. "
            f"WatchdogConfig.kill_grace_s is parsed and documented as the TERM->KILL "
            f"grace and Watchdog.escalate_pending_groups exists to spend it, but "
            f"nothing in the package ever calls that method, so a stuck stage is "
            f"only ever TERMed."
        )
    finally:
        _cleanup(sup, identity)
