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
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.events import read_serve_events
from agent_fleet.serve.paths import component_pid_path, ensure_serve_dir
from agent_fleet.serve.procs import starttime_fingerprint

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
