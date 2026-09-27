"""Supervision: restart, backoff, crash-loop, and never double-start.

These use **real child processes**, not an injected runner. That is not
ceremony: crash detection is "the child exited", which a mock can only assert
about itself, and the re-attach and no-double-start properties are about real
process identity — a pid file, a /proc fingerprint, and an flock. A fake runner
proves the code reads its own return value; a real child proves the behaviour.

The properties pinned here, and what each costs when it is wrong:

* **a crash-looping component is stopped, and the others keep running** — a
  single broken component must not take the fleet down;
* **a requested restart does not consume the crash budget** — otherwise a
  component the watchdog keeps restarting for making no progress is eventually
  declared crash-looping for a fault it never had;
* **the crash budget survives a supervisor restart** — otherwise the detector
  resets exactly when it is most needed;
* **adoption requires a matching fingerprint** — a recycled pid must never be
  adopted, and therefore never later killed;
* **two supervisors cannot both run** — the double-dispatch failure mode.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import read_serve_events
from agent_fleet.serve.paths import component_pid_path, ensure_serve_dir
from agent_fleet.serve.procs import TerminationResult, starttime_fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable

from agent_fleet.serve.supervisor import (
    STATE_BACKOFF,
    STATE_CRASH_LOOPING,
    STATE_RUNNING,
    STATE_STOPPED,
    Supervisor,
    expand_command,
)

#: Real commands, not snippets: serve execs its argv without a shell, so these
#: must be executable names. A child that exits 3 immediately (a crash).
CRASHER = f"{sys.executable} -c 'import sys; sys.exit(3)'"
#: A child that runs until killed.
SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _config(
    *,
    dispatcher: str | None = None,
    merger: str | None = None,
    janitor: str | None = None,
    crash_threshold: int = 3,
    crash_window_minutes: int = 15,
    backoff_initial_s: float = 1.0,
    backoff_max_s: float = 60.0,
    no_progress_restarts: int = 2,
) -> ServeConfig:
    def spec(name: str, command: str | None) -> ComponentSpec:
        return ComponentSpec(
            name=name,
            command=command,
            backoff_initial_s=backoff_initial_s,
            backoff_max_s=backoff_max_s,
            crash_threshold=crash_threshold,
            crash_window_minutes=crash_window_minutes,
            no_progress_restarts=no_progress_restarts,
            no_progress_window_minutes=30,
        )

    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        shutdown_grace_s=1.0,
        components={
            "dispatcher": spec("dispatcher", dispatcher),
            "merger": spec("merger", merger),
            "janitor": spec("janitor", janitor),
        },
        watchdog=WatchdogConfig(),
    )


def _supervisor(config: ServeConfig, clock: FakeClock) -> Supervisor:
    return Supervisor("op", config, clock=clock)


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def spawn_failures(operator: str) -> list[dict[str, object]]:
    """Every ``spawn_failed`` event recorded for *operator*, oldest first."""
    return [e for e in read_serve_events(operator) if e["event"] == "serve.component.spawn_failed"]


# ------------------------------------------------------------------- templates


def test_expand_command_substitutes_every_documented_placeholder(tmp_path: Path) -> None:
    targets = {
        "max_lanes": 7,
        "max_gates": 4,
        "test_pool": 3,
        "typecheck_pool": 2,
        "gates_priority": True,
    }
    argv = expand_command(
        "dispatch --operator {operator} --max {max_lanes} --gates {max_gates} "
        "--cap {capacity_file} --dir {serve_dir} --prio {gates_priority}",
        operator="documents-0e",
        serve_dir=tmp_path,
        capacity_file=tmp_path / "capacity.json",
        targets=targets,
    )
    assert argv[0] == "dispatch"
    assert "--operator" in argv and "documents-0e" in argv
    assert "7" in argv and "4" in argv and str(tmp_path / "capacity.json") in argv
    assert argv[-1] == "1", "gates_priority=True must project as 1"


def test_expand_command_survives_braces_in_the_template() -> None:
    """Plain replacement, not str.format.

    A component command routinely contains a brace — a jq filter, a python
    one-liner — and str.format would raise on it.
    """
    argv = expand_command(
        "sh -c 'echo {not_a_placeholder}'",
        operator="op",
        serve_dir=Path("/tmp"),
        capacity_file=Path("/tmp/c.json"),
        targets={},
    )
    assert any("{not_a_placeholder}" in arg for arg in argv), "the brace must survive"


def test_expand_command_splits_a_shell_command_line() -> None:
    argv = expand_command(
        "python -c 'import time; time.sleep(1)'",
        operator="op",
        serve_dir=Path("/tmp"),
        capacity_file=Path("/tmp/c.json"),
        targets={},
    )
    assert argv[0] == "python"
    assert "-c" in argv
    assert argv[-1] == "import time; time.sleep(1)"


def test_expand_command_with_no_placeholders_is_verbatim(tmp_path: Path) -> None:
    argv = expand_command(
        "true", operator="op", serve_dir=tmp_path, capacity_file=tmp_path / "c", targets={}
    )
    assert argv == ["true"]


# ---------------------------------------------------------------------- start


def test_a_disabled_component_is_never_started() -> None:
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=None), clock)
    assert sup.start("dispatcher") is False
    assert "dispatcher" not in sup._procs


def test_start_spawns_a_real_child_and_records_its_fingerprint() -> None:
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=SLEEPER), clock)
    try:
        assert sup.start("dispatcher") is True
        state = sup.children["dispatcher"]
        assert state.state == STATE_RUNNING
        assert state.pid is not None and state.pid > 0
        assert state.starttime == starttime_fingerprint(state.pid)
        payload = component_pid_path("op", "dispatcher")
        assert payload.exists(), "the pid file is what makes re-attach possible"
    finally:
        sup.shutdown()
    assert not component_pid_path("op", "dispatcher").exists(), "pid file must be cleared"


def test_start_is_idempotent_and_never_double_starts() -> None:
    """The double-dispatch failure mode.

    Two starts of the same component must leave exactly one process: the second
    adopts the first rather than spawning a rival that would fight it over the
    worktree.
    """
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=SLEEPER), clock)
    try:
        sup.start("dispatcher")
        first_pid = sup.children["dispatcher"].pid
        sup.start("dispatcher")
        assert sup.children["dispatcher"].pid == first_pid
        assert len(sup._procs) == 1
    finally:
        sup.shutdown()


def test_a_child_spawn_failure_is_recorded_not_raised() -> None:
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher="definitely-not-a-real-binary-xyz"), clock)
    assert sup.start("dispatcher") is False
    events = [e["event"] for e in read_serve_events("op")]
    assert "serve.component.spawn_failed" in events


def test_an_empty_command_is_an_error_event_not_a_spawn() -> None:
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher="   "), clock)
    assert sup.start("dispatcher") is False


def test_a_command_that_cannot_be_exec_is_retried_on_a_backoff_not_every_tick() -> None:
    """A spawn that never became a process still owes the backoff schedule.

    ``Popen`` raising means there is no child, so the reap loop — the only thing
    that used to book a backoff — never sees it. The component was left
    ``stopped``, which is the state ``tick``'s ensure-running loop re-attempts
    immediately, so a command that is simply not on PATH was exec'd again on
    every single tick: one failed ``Popen`` and one appended ``spawn_failed``
    event per tick, forever, with the crash-loop detector never consulted.
    """
    clock = FakeClock()
    sup = _supervisor(
        _config(dispatcher="definitely-not-a-real-binary-xyz", backoff_initial_s=5.0),
        clock,
    )
    try:
        for _ in range(50):
            sup.tick()

        state = sup.children["dispatcher"]
        assert state.state == STATE_BACKOFF, (
            f"a component whose command cannot be exec'd must wait out a backoff, "
            f"not sit in {state.state!r} where every tick re-attempts the exec"
        )
        assert state.restart_at > 0, "the retry has to carry a deadline, or it is now"
        assert state.restarts >= 1, "the failed attempt must count against the schedule"

        # The same fault, repeated, still has to reach the crash budget — the
        # defect was never that it retried, it was that nothing bounded it.
        assert len(spawn_failures("op")) <= 3, (
            f"{len(spawn_failures('op'))} failed execs across 50 ticks "
            "(crash_threshold is 3). One "
            "failed exec per tick is unbounded: the serve loop ticks ~60 times a "
            "second, so a typo in a command template became ~60 failed Popen calls "
            "and a matching amount of appended event-log growth every second"
        )
    finally:
        sup.shutdown()


def test_a_command_that_cannot_be_exec_eventually_declares_crash_looping() -> None:
    """The budget is consulted on the spawn path too, not only on the exit path.

    Without this a component that could never be started looked infinitely
    retryable: no backoff was charged, so the detector — which only ever ran on
    the reap path — never saw the crashes, and the documented
    ``serve.component.crash_loop`` alert was never emitted for the one fault
    that most needs an operator to look at it.
    """
    clock = FakeClock()
    sup = _supervisor(
        _config(dispatcher="definitely-not-a-real-binary-xyz", crash_threshold=3),
        clock,
    )
    try:
        for _ in range(200):
            sup.tick()
            # A real serve loop paces itself; release each backoff as it falls due.
            clock.advance(sup.pending_restarts() + 0.01)

        state = sup.children["dispatcher"]
        assert state.state == STATE_CRASH_LOOPING, (
            f"after 200 ticks of a command that cannot be exec'd the component is "
            f"{state.state!r}, not crash_looping: the spawn failure never consulted "
            "the crash budget, so the fault repeats forever instead of being "
            "handed to a human"
        )
        alerts = [e for e in read_serve_events("op") if e["event"] == "serve.component.crash_loop"]
        assert alerts, "a component that will not start must raise the crash-loop alert"
        assert alerts[-1]["level"] == "error"
        assert alerts[-1]["data"]["component"] == "dispatcher"

        # And it must stay down: a spent budget is what stops the spin.
        attempts = len(spawn_failures("op"))
        for _ in range(50):
            clock.advance(600.0)
            sup.tick()
        assert len(spawn_failures("op")) == attempts, (
            "a crash-looping component must not be re-exec'd; the budget is spent"
        )
    finally:
        sup.shutdown()


# -------------------------------------------------------------------- adoption


def test_adoption_works_when_the_fingerprint_still_matches() -> None:
    """A restarting supervisor attaches to its own still-running child."""
    first = _supervisor(_config(dispatcher=SLEEPER), FakeClock())
    first.start("dispatcher")
    pid = first.children["dispatcher"].pid
    try:
        # A second supervisor, sharing the same state directory, re-attaches.
        second = _supervisor(_config(dispatcher=SLEEPER), FakeClock())
        assert second.adopt("dispatcher") is True
        assert second.children["dispatcher"].pid == pid
        assert second.children["dispatcher"].adopted is True
        second.save()
        second.shutdown()  # must not kill the adopted child
        assert first.children["dispatcher"].pid == pid
    finally:
        first.shutdown()


def test_adoption_refuses_a_pid_whose_fingerprint_does_not_match() -> None:
    """A recycled pid must never be adopted.

    Adopting a stranger's pid means supervising it forever, and eventually
    killing it when the supervisor decides it is wedged.
    """
    sup = _supervisor(_config(dispatcher=SLEEPER), FakeClock())
    ensure_serve_dir("op")
    from agent_fleet.serve.paths import write_json_atomic

    write_json_atomic(
        component_pid_path("op", "dispatcher"),
        {"component": "dispatcher", "pid": os.getpid(), "starttime": 12345},
    )
    assert sup.adopt("dispatcher") is False


def test_adoption_refuses_when_the_recorded_process_is_gone() -> None:
    sup = _supervisor(_config(dispatcher=SLEEPER), FakeClock())
    from agent_fleet.serve.paths import write_json_atomic

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    write_json_atomic(
        component_pid_path("op", "dispatcher"),
        {
            "component": "dispatcher",
            "pid": dead.pid,
            "starttime": starttime_fingerprint(dead.pid) or 1,
        },
    )
    assert sup.adopt("dispatcher") is False


def test_adoption_of_a_missing_pid_file_is_false() -> None:
    sup = _supervisor(_config(dispatcher=SLEEPER), FakeClock())
    assert sup.adopt("dispatcher") is False


# --------------------------------------------------------------------- backoff


def test_backoff_is_exponential_and_capped() -> None:
    sup = _supervisor(
        _config(dispatcher=CRASHER, backoff_initial_s=2.0, backoff_max_s=10.0), FakeClock()
    )
    expected = [2.0, 4.0, 8.0, 10.0, 10.0]
    actual: list[float] = []
    for _ in range(5):
        sup.children.setdefault("dispatcher", _blank("dispatcher")).restarts += 1
        actual.append(sup.backoff_for("dispatcher"))
    assert actual == expected


def test_a_crashing_child_is_restarted_with_backoff() -> None:
    """A real child that exits 3 must come back, and the wait must be recorded."""
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=CRASHER, crash_threshold=10, backoff_initial_s=2.0), clock)
    try:
        sup.start("dispatcher")
        first = sup.children["dispatcher"].pid
        assert first is not None
        # tick() is what reaps and restarts; drive it until the pid changes.
        restarted = _wait_until(lambda: _tick_until_restarted(sup, first))
        assert restarted, "a crashing child must come back"
        assert sup.children["dispatcher"].restarts >= 2
        assert clock.slept, "a restart must wait, not hot-loop"
        assert clock.slept[0] == pytest.approx(2.0)
    finally:
        sup.shutdown()


def test_a_requested_restart_does_not_consume_the_crash_budget() -> None:
    """Watchdog-driven restarts must not look like crashes.

    Otherwise a component the watchdog keeps restarting for making no progress
    eventually trips the crash-loop detector and is stopped for a fault it
    never had — and the real problem is hidden behind a crash-loop alert.
    """
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=SLEEPER, crash_threshold=3), clock)
    try:
        sup.start("dispatcher")
        for _ in range(6):
            sup.request_restart("dispatcher", reason="no progress")
            time.sleep(0.05)
        state = sup.children["dispatcher"]
        assert state.crashes_in_window(clock.time(), 900.0) == 0
        assert state.state != STATE_CRASH_LOOPING
    finally:
        sup.shutdown()


def test_a_requested_restart_still_restarts_the_component() -> None:
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=SLEEPER), clock)
    try:
        sup.start("dispatcher")
        first = sup.children["dispatcher"].pid
        sup.request_restart("dispatcher", reason="wedged")
        assert sup.children["dispatcher"].pid != first
    finally:
        sup.shutdown()


#: Ignores the TERM outright, so only the group KILL can stop it.
TRAPPER = (
    f"{sys.executable} -c 'import signal, time; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'"
)

#: Generous enough that a loaded box is not what makes the trap late.
_TRAPPER_READY_S = 1.0


def live_component_pids(sup: Supervisor) -> list[int]:
    """Every live process the supervisor still believes is running a component.

    Read from ``/proc`` rather than from the supervisor's own ledger: the ledger
    is the thing under test here, and a duplicate is invisible to whichever copy
    of the bookkeeping forgot about it.
    """
    from agent_fleet.serve.procs import pid_alive

    return [
        pid for pid in (s.pid for s in sup.children.values()) if pid is not None and pid_alive(pid)
    ]


def test_a_component_that_survives_a_stop_is_not_double_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart must never add a rival to a process that is still alive.

    ``stop_component`` escalates a trapped TERM to a group KILL, but a KILL the
    escalation cannot deliver — refused because the pid is no longer a group
    leader, or already gone from under us — leaves the component running.
    ``_await_exit`` reports exactly that, and the verdict was being thrown away:
    the pid file was cleared and ``state.pid`` nulled regardless, so the
    follow-up ``start`` re-ran its adoption check against a pid file that no
    longer existed, found no proof, and spawned a *second* process for a role
    that was still occupied. The original was no longer in the children ledger,
    so nothing could ever reach it again.

    Asserted on a real process, and on the live set rather than on a return
    value, because "one live process per role" is the property that broke. The
    escalation is disabled with ``monkeypatch`` because the escape hatch has to
    be genuinely unreachable: a real SIGKILL lands, and a test that depends on
    the kernel declining to kill a process proves nothing.
    """
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=TRAPPER), clock)
    skipped = TerminationResult(signalled=False, skipped_reason="pid is not a group leader")

    monkeypatch.setattr(
        "agent_fleet.serve.supervisor.escalate_kill_group",
        lambda *_args, **_kwargs: skipped,
    )

    proc: subprocess.Popen[bytes] | None = None
    try:
        sup.start("dispatcher")
        original = sup.children["dispatcher"].pid
        assert original is not None
        proc = sup._procs["dispatcher"]
        # The component has to be *up* before it is asked to stop. Under a
        # ``FakeClock`` the TERM grace burns out in microseconds of real time,
        # so a stop issued during interpreter startup arrives before the trap is
        # installed and kills the child outright — which would make this test
        # pass for the wrong reason. Bounded and real, because the thing being
        # waited for is the kernel, not the injected clock.
        assert _wait_until(lambda: proc.poll() is None, 5.0)
        time.sleep(_TRAPPER_READY_S)

        assert sup.request_restart("dispatcher", reason="no progress") is False, (
            "a restart whose stop did not confirm the exit must not report success: "
            "the component it could not stop is still the one running"
        )

        state = sup.children["dispatcher"]
        assert state.pid == original, (
            f"the surviving component's identity was discarded (pid is now {state.pid}, "
            f"was {original}); nothing can signal a process serve no longer tracks"
        )
        assert state.state == STATE_RUNNING, (
            f"a process that is still alive is reported as {state.state!r}"
        )
        assert component_pid_path("op", "dispatcher").exists(), (
            "the pid file was cleared while its process is alive, so the next start "
            "cannot prove adoption and spawns a rival"
        )

        # The follow-up start is the call that actually doubled the process.
        assert sup.start("dispatcher", cause="requested") is True
        assert sup.children["dispatcher"].pid == original, (
            "start() spawned a replacement for a role that is still occupied; the "
            "adoption check had no pid file left to prove otherwise"
        )
        assert sup.children["dispatcher"].adopted is True
        assert live_component_pids(sup) == [original], (
            "exactly one live process must exist for the role, so a restart cannot "
            f"put a second dispatcher on the same work; found {live_component_pids(sup)}"
        )
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)


# ----------------------------------------------------------------- crash loops


def test_crash_looping_stops_the_component_and_alerts() -> None:
    """After N crashes in M minutes: stop restarting, alert, keep the others up."""
    clock = FakeClock()
    config = _config(dispatcher=CRASHER, merger=SLEEPER, crash_threshold=1, crash_window_minutes=15)
    sup = _supervisor(config, clock)
    try:
        sup.start("dispatcher")
        sup.start("merger")
        # Wait for the real child to exit, then tick: the detector must classify
        # the non-zero exit as a crash and find the budget already spent.
        proc = sup._procs["dispatcher"]
        assert _wait_until(lambda: proc.poll() is not None)
        sup.tick()
        state = sup.children["dispatcher"]
        assert state.last_exit_cause == "crash", "a non-zero exit is a crash"
        assert state.state == STATE_CRASH_LOOPING
        assert "not restarting" in state.message
        events = [e for e in read_serve_events("op") if e["event"] == "serve.component.crash_loop"]
        assert events, "a crash loop must raise an alert event"
        assert events[-1]["level"] == "error"
        assert events[-1]["data"]["component"] == "dispatcher"
        # The other component is untouched: one broken piece must not stop the fleet.
        assert sup.children["merger"].state == STATE_RUNNING
    finally:
        sup.shutdown()


def test_a_crash_loopped_component_is_not_started_again() -> None:
    """Once the budget is spent, the component stays down until a human looks."""
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=CRASHER, crash_threshold=1), clock)
    try:
        sup.start("dispatcher")
        proc = sup._procs["dispatcher"]
        assert _wait_until(lambda: proc.poll() is not None)
        sup.tick()
        assert sup.children["dispatcher"].state == STATE_CRASH_LOOPING
        assert sup._crash_looping("dispatcher")
        assert sup.start("dispatcher") is False
        assert sup.request_restart("dispatcher", reason="please") is False
    finally:
        sup.shutdown()


def test_crash_history_survives_a_supervisor_restart() -> None:
    """Otherwise restarting serve hands a crash-looping component a fresh budget.

    The detector exists for a supervisor that has been running long enough for
    something to be genuinely broken; resetting its memory on every restart
    disables it in exactly that case.
    """
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=CRASHER, crash_threshold=2), clock)
    sup.children["dispatcher"] = _blank("dispatcher")
    sup.children["dispatcher"].crash_epochs = [clock.time() - 10, clock.time() - 5]
    sup.children["dispatcher"].restarts = 2
    sup.save()

    resumed = _supervisor(_config(dispatcher=CRASHER, crash_threshold=2), clock)
    assert len(resumed.children["dispatcher"].crash_epochs) == 2
    assert resumed._crash_looping("dispatcher")
    resumed.children["dispatcher"].crash_epochs.append(clock.time())
    resumed.save()
    third = _supervisor(_config(dispatcher=CRASHER, crash_threshold=2), clock)
    assert third._crash_looping("dispatcher")


def _tick_until_restarted(sup: Supervisor, first_pid: int) -> bool:
    """Tick until the child is replaced. Returns True once it has been."""
    proc = sup._procs.get("dispatcher")
    if proc is not None and proc.poll() is None:
        return False
    sup.tick()
    return sup.children["dispatcher"].pid != first_pid


def _blank(name: str):  # noqa: ANN202
    from agent_fleet.serve.supervisor import ChildState

    return ChildState(name=name)


def test_state_roundtrips_through_disk() -> None:
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=SLEEPER, merger=CRASHER), clock)
    sup.children["dispatcher"] = _blank("dispatcher")
    sup.children["dispatcher"].restarts = 7
    sup.children["dispatcher"].last_exit_cause = "crash"
    sup.children["dispatcher"].last_exit_code = 3
    sup.children["merger"] = _blank("merger")
    sup.children["merger"].last_exit_code = 3
    sup.save()
    resumed = _supervisor(_config(dispatcher=SLEEPER, merger=CRASHER), clock)
    assert resumed.children["dispatcher"].restarts == 7
    assert resumed.children["dispatcher"].last_exit_cause == "crash"
    assert resumed.children["merger"].last_exit_code == 3


# ------------------------------------------------------------------- shutdown


def test_shutdown_terminates_children_and_keeps_no_orphans() -> None:
    from agent_fleet.serve.procs import pid_alive

    sup = _supervisor(_config(dispatcher=SLEEPER, merger=SLEEPER), FakeClock())
    sup.start("dispatcher")
    sup.start("merger")
    pids = [s.pid for s in sup.children.values() if s.pid]
    sup.shutdown()
    assert all(not pid_alive(pid) for pid in pids), "shutdown must not leave children running"


def test_shutdown_is_idempotent() -> None:
    sup = _supervisor(_config(dispatcher=SLEEPER), FakeClock())
    sup.start("dispatcher")
    sup.shutdown()
    sup.shutdown()


def test_tick_after_shutdown_does_nothing() -> None:
    clock = FakeClock()
    sup = _supervisor(_config(dispatcher=SLEEPER), clock)
    sup.start("dispatcher")
    sup.shutdown()
    sup.tick()
    assert sup.children["dispatcher"].state == STATE_STOPPED


# --------------------------------------------------------------------- status


def test_status_rows_cover_every_component_with_restarts_and_state() -> None:
    sup = _supervisor(_config(dispatcher=SLEEPER, merger=None), FakeClock())
    try:
        sup.start("dispatcher")
        rows = {r["component"]: r for r in sup.status_rows()}
        assert set(rows) == {"dispatcher", "merger", "janitor"}
        assert rows["dispatcher"]["state"] == STATE_RUNNING
        assert rows["dispatcher"]["enabled"] is True
        assert rows["merger"]["enabled"] is False
        assert rows["janitor"]["enabled"] is False
        assert rows["dispatcher"]["restarts"] >= 1
    finally:
        sup.shutdown()


def test_component_backoff_state_constant_is_used() -> None:
    """The state the operator sees between an exit and a restart is named."""
    assert STATE_BACKOFF != STATE_RUNNING
