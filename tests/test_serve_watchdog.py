"""Watchdog rules and the lock records that make two of them possible.

Rules (c) stale-lock and (d) deadlock have no data model without
:mod:`agent_fleet.serve.locks`. ``flock`` is mutually exclusive but completely
unobservable: the lock file has no content, so when a merge stopped holding,
nothing could say who had it or how long, and the deadlock had to be diagnosed
by a human reading a log. These tests pin the records *and* the remediations.

The load-bearing safety property, tested directly: a remediation only ever acts
on a pid in the supervisor's own ledger with a matching start-time
fingerprint. A ``ppid == 1`` predicate on its own is satisfied by every
reparented process on the box, including other operators' agents, so the
tests show that adding a stranger's pid to the system changes nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.locks import STATE_FREE, STATE_HELD, STATE_WAITING, LockRecord, LockRegistry
from agent_fleet.serve.paths import component_log_path, ensure_serve_dir, write_json_atomic
from agent_fleet.serve.procs import starttime_fingerprint
from agent_fleet.serve.supervisor import ChildState, Supervisor
from agent_fleet.serve.watchdog import (
    RULE_DEADLOCK,
    RULE_NO_PROGRESS,
    RULE_ORPHAN,
    RULE_STALE_LOCK,
    RULE_STUCK_STAGE,
    Watchdog,
    WatchdogReport,
    growth_idle_seconds,
)

SLEEPER = f"{sys.executable} -c 'import time; time.sleep(300)'"
DEAD_PID = 999_999


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@dataclass
class _FakeStat:
    st_mtime: float


def _dead_proc_root(tmp_path: Path) -> Path:
    """A /proc in which a chosen pid is alive and every other pid is gone.

    Lets the lock rules be exercised against a real supervisor holding a real
    child while the *other* side of a relationship is provably dead.
    """
    root = tmp_path / "proc"
    for pid in (1, os.getpid()):
        (root / str(pid)).mkdir(parents=True, exist_ok=True)
        fields = ["S", "1"] + ["0"] * 18
        fields[19] = "1000"
        (root / str(pid) / "stat").write_text(
            f"{pid} (proc) {' '.join(fields)}\n", encoding="utf-8"
        )
    (root / "stat").write_text("btime 1000000\n", encoding="utf-8")
    return root


def _supervisor_with_child(
    command: str = SLEEPER,
    *,
    stage_timeout_minutes: dict[str, int] | None = None,
    stage_retry_budget: int = 1,
) -> Supervisor:
    """A supervisor with one live child, and the same config the watchdog gets.

    The watchdog reads its restart budget from ``supervisor.config``, so the
    supervisor must be built with the config under test — otherwise the test
    silently asserts against the default budget instead of the one it set.
    """
    config = _watchdog_config(
        command=command,
        stage_timeout_minutes=stage_timeout_minutes,
        stage_retry_budget=stage_retry_budget,
    )
    sup = Supervisor("op", config, clock=FakeClock())
    sup.start("dispatcher")
    return sup


def _watchdog_config(
    *,
    command: str = SLEEPER,
    stage_timeout_minutes: dict[str, int] | None = None,
    stage_retry_budget: int = 1,
    no_progress_minutes: int = 30,
    orphan_minutes: int = 60,
    stale_lock_minutes: int = 15,
    deadlock_minutes: int = 20,
    max_remediations_per_tick: int = 5,
    no_progress_restarts: int = 2,
    no_progress_window_minutes: int = 30,
) -> ServeConfig:
    return ServeConfig(
        operator="op",
        components={
            "dispatcher": ComponentSpec(
                name="dispatcher",
                command=command,
                no_progress_restarts=no_progress_restarts,
                no_progress_window_minutes=no_progress_window_minutes,
            )
        },
        watchdog=WatchdogConfig(
            stage_timeout_minutes=stage_timeout_minutes or {"lane": 120},
            stage_retry_budget=stage_retry_budget,
            no_progress_minutes=no_progress_minutes,
            orphan_minutes=orphan_minutes,
            stale_lock_minutes=stale_lock_minutes,
            deadlock_minutes=deadlock_minutes,
            max_remediations_per_tick=max_remediations_per_tick,
        ),
    )


# ------------------------------------------------------------- growth detector


def test_growth_idle_seconds_uses_the_injected_mtime() -> None:
    assert growth_idle_seconds(Path("/x"), now=1000.0, stat=_FakeStat(st_mtime=940.0)) == 60.0


def test_growth_idle_seconds_is_infinite_for_a_missing_file(tmp_path: Path) -> None:
    """A stage that has produced nothing is maximally stuck.

    Returning zero would exempt exactly the worst case — the agent that never
    started writing at all.
    """
    assert growth_idle_seconds(tmp_path / "never-written", now=1000.0) == float("inf")


def test_growth_idle_seconds_never_goes_negative(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("x", encoding="utf-8")
    assert growth_idle_seconds(path, now=0.0, stat=_FakeStat(st_mtime=500.0)) == 0.0


# ---------------------------------------------------------------- lock records


def test_lock_records_survive_a_roundtrip() -> None:
    registry = LockRegistry("op")
    registry.mark_held("repo:lake", holder="merger", pid=42, starttime=99, now=1000.0)
    record = registry.read("repo:lake")
    assert record is not None
    assert record.state == STATE_HELD
    assert record.holder == "merger"
    assert record.identity is not None and record.identity.pid == 42


def test_lock_record_from_dict_rejects_nameless_payloads() -> None:
    assert LockRecord.from_dict({}) is None
    assert LockRecord.from_dict({"name": ""}) is None
    parsed = LockRecord.from_dict({"name": "x", "pid": 5, "starttime": 6, "acquired_epoch": 7.0})
    assert parsed is not None and parsed.acquired_epoch == 7.0


def test_lock_record_without_a_fingerprint_has_no_identity() -> None:
    record = LockRecord(name="x", state=STATE_HELD, pid=5)
    assert record.identity is None


def test_hold_records_intent_and_releases() -> None:
    registry = LockRegistry("op")
    with registry.hold("repo:lake", holder="merger", pid=os.getpid(), wanting="merge PR 1") as got:
        assert got is True
        record = registry.read("repo:lake")
        assert record is not None
        assert record.state == STATE_HELD
        assert record.wanting == "merge PR 1"
    after = registry.read("repo:lake")
    assert after is not None and after.state == STATE_FREE


def test_hold_records_a_waiting_edge_when_the_lock_is_taken() -> None:
    """The edge must exist even though the acquire failed.

    Writing the record only on success would record successes, and a deadlock
    is made entirely of failures.
    """
    registry = LockRegistry("op")
    with registry.hold("repo:lake", holder="merger", pid=os.getpid()):
        other = LockRegistry("op")
        # A second holder in this process would deadlock on flock; instead drive
        # the waiting record directly, which is what a foreign component does.
        other.mark_waiting(
            "repo:lake",
            holder="dispatcher",
            pid=os.getpid(),
            starttime=1,
            waiting_for=None,
            wanting="launch lane",
        )
        record = other.read("repo:lake")
        assert record is not None
        assert record.state == STATE_WAITING
        assert record.wanting == "launch lane"


def test_release_reports_the_previous_record() -> None:
    registry = LockRegistry("op")
    registry.mark_held("a", holder="x", pid=1, starttime=1, now=0.0)
    previous = registry.release("a", note="done")
    assert previous is not None and previous.holder == "x"
    after = registry.read("a")
    assert after is not None
    assert after.note == "done"


def test_release_of_an_unknown_lock_is_none() -> None:
    assert LockRegistry("op").release("never-seen") is None


def test_forget_removes_the_record_file() -> None:
    registry = LockRegistry("op")
    registry.mark_held("a", holder="x", pid=1, starttime=1, now=0.0)
    assert registry.forget("a") is True
    assert registry.read("a") is None
    assert registry.forget("a") is False


# ------------------------------------------------------ rule (c) stale locks


def test_a_lock_whose_holder_is_dead_is_stale(tmp_path: Path) -> None:
    proc_root = _dead_proc_root(tmp_path)
    registry = LockRegistry("op", proc_root=proc_root)
    registry.mark_held("repo:lake", holder="merger", pid=DEAD_PID, starttime=1, now=0.0)
    stale = registry.stale_locks(now=3600.0, grace_minutes=1)
    assert [r.name for r in stale] == ["repo:lake"]


def test_a_freshly_dead_holder_is_not_yet_stale(tmp_path: Path) -> None:
    """A lock whose holder died a second ago is not stolen.

    Releasing it immediately would let a second component into a critical
    section the first is still finishing its exit from.
    """
    proc_root = _dead_proc_root(tmp_path)
    registry = LockRegistry("op", proc_root=proc_root)
    registry.mark_held("repo:lake", holder="merger", pid=DEAD_PID, starttime=1, now=0.0)
    assert registry.stale_locks(now=30.0, grace_minutes=1) == []


def test_a_live_holder_is_never_stale(tmp_path: Path) -> None:
    proc_root = _dead_proc_root(tmp_path)
    registry = LockRegistry("op", proc_root=proc_root)
    registry.mark_held("repo:lake", holder="merger", pid=os.getpid(), starttime=1, now=0.0)
    assert registry.stale_locks(now=9999.0, grace_minutes=1) == []


def test_the_watchdog_releases_a_stale_lock_and_records_it(tmp_path: Path) -> None:
    proc_root = _dead_proc_root(tmp_path)
    sup = Supervisor("op", ServeConfig(operator="op"), clock=FakeClock(), proc_root=proc_root)
    registry = LockRegistry("op", proc_root=proc_root)
    registry.mark_held("repo:lake", holder="merger", pid=DEAD_PID, starttime=1, now=0.0)
    watchdog = Watchdog(
        "op",
        _watchdog_config(stale_lock_minutes=1),
        sup,
        clock=FakeClock(),
        proc_root=proc_root,
        locks=registry,
    )
    report = watchdog.tick()
    assert [r.rule for r in report.remediations] == [RULE_STALE_LOCK]
    record = registry.read("repo:lake")
    assert record is not None and record.state == STATE_FREE
    assert "watchdog" in record.note


def test_a_dry_run_reports_the_stale_lock_without_releasing_it(tmp_path: Path) -> None:
    proc_root = _dead_proc_root(tmp_path)
    sup = Supervisor("op", ServeConfig(operator="op"), clock=FakeClock(), proc_root=proc_root)
    registry = LockRegistry("op", proc_root=proc_root)
    registry.mark_held("repo:lake", holder="merger", pid=DEAD_PID, starttime=1, now=0.0)
    watchdog = Watchdog(
        "op",
        _watchdog_config(stale_lock_minutes=1),
        sup,
        clock=FakeClock(),
        proc_root=proc_root,
        locks=registry,
        dry_run=True,
    )
    report = watchdog.tick()
    assert [r.rule for r in report.remediations] == [RULE_STALE_LOCK]
    still = registry.read("repo:lake")
    assert still is not None and still.state == STATE_HELD, "a dry run must not act"


# ---------------------------------------------------------- rule (d) deadlock


def test_a_two_way_wait_cycle_is_detected(tmp_path: Path) -> None:
    """The graph edge is the point: flock alone cannot express this."""
    proc_root = _dead_proc_root(tmp_path)
    registry = LockRegistry("op", proc_root=proc_root)
    # dispatcher waits for the merge lock; merger waits for the dispatch lock.
    registry.mark_held("merge", holder="merger", pid=os.getpid(), starttime=1, now=0.0)
    registry.mark_waiting(
        "merge",
        holder="dispatcher",
        pid=os.getpid(),
        starttime=1,
        waiting_for="dispatch",
        wanting="merge PR",
        now=0.0,
    )
    registry.mark_held("dispatch", holder="dispatcher", pid=os.getpid(), starttime=1, now=0.0)
    registry.mark_waiting(
        "dispatch",
        holder="merger",
        pid=os.getpid(),
        starttime=1,
        waiting_for="merge",
        wanting="launch lane",
        now=0.0,
    )
    cycles = registry.deadlocks(now=3600.0, threshold_minutes=1)
    assert len(cycles) == 1
    names = {r.name for r in cycles[0]}
    assert names == {"merge", "dispatch"}


def test_a_young_cycle_is_not_yet_a_deadlock(tmp_path: Path) -> None:
    proc_root = _dead_proc_root(tmp_path)
    registry = LockRegistry("op", proc_root=proc_root)
    registry.mark_held("merge", holder="merger", pid=1, starttime=1, now=0.0)
    registry.mark_waiting(
        "merge", holder="dispatcher", pid=1, starttime=1, waiting_for="merge", now=0.0
    )
    assert registry.deadlocks(now=10.0, threshold_minutes=30) == []


def test_the_watchdog_releases_the_older_claim_in_a_cycle(tmp_path: Path) -> None:
    proc_root = _dead_proc_root(tmp_path)
    sup = Supervisor("op", ServeConfig(operator="op"), clock=FakeClock(), proc_root=proc_root)
    registry = LockRegistry("op", proc_root=proc_root)
    registry.mark_waiting(
        "dispatch",
        holder="dispatcher",
        pid=1,
        starttime=1,
        waiting_for="merge",
        wanting="merge PR 1",
        now=0.0,
    )
    registry.mark_held("merge", holder="merger", pid=1, starttime=1, now=0.0)
    registry.mark_waiting(
        "merge",
        holder="merger",
        pid=1,
        starttime=1,
        waiting_for="dispatch",
        wanting="launch lane 2",
        now=0.0,
    )
    registry.mark_held("dispatch", holder="dispatcher", pid=1, starttime=1, now=0.0)
    watchdog = Watchdog(
        "op",
        _watchdog_config(deadlock_minutes=1),
        sup,
        clock=FakeClock(),
        proc_root=proc_root,
        locks=registry,
    )
    report = watchdog.tick()
    rules = {r.rule for r in report.remediations}
    assert RULE_DEADLOCK in rules, f"expected a deadlock remediation, got {rules}"
    assert any(r.rule == RULE_DEADLOCK and "cycle" in r.reason for r in report.remediations)
    released = registry.read("merge")
    assert released is not None and released.state == STATE_FREE


# --------------------------------------------------- rules (a) and (b) on pids


def test_a_stuck_component_with_a_stale_log_is_terminated() -> None:
    clock = FakeClock()
    sup = _supervisor_with_child(stage_timeout_minutes={"lane": 1})
    try:
        # A log whose mtime is an hour old, and a 1-minute stage timeout.
        _backdate_log(clock)
        sup = _supervisor_with_child(stage_timeout_minutes={"lane": 1})
        config = _watchdog_config(stage_timeout_minutes={"lane": 1})
        watchdog = Watchdog("op", config, sup, clock=clock)
        report = watchdog.tick()
        stuck = [r for r in report.remediations if r.rule == RULE_STUCK_STAGE]
        assert stuck, "a stage with no output growth past its timeout must be acted on"
        assert stuck[0].signalled is True
    finally:
        sup.shutdown()


def test_a_healthy_component_is_left_alone() -> None:
    clock = FakeClock()
    sup = _supervisor_with_child()
    try:
        log = component_log_path("op", "dispatcher")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fresh", encoding="utf-8")
        report = Watchdog(
            "op", _watchdog_config(stage_timeout_minutes={"lane": 60}), sup, clock=clock
        ).tick()
        assert [r for r in report.remediations if r.rule == RULE_STUCK_STAGE] == []
    finally:
        sup.shutdown()


def test_a_component_with_no_log_is_not_assumed_stuck() -> None:
    """A detector that fires on missing evidence fires on everything."""
    clock = FakeClock()
    sup = Supervisor(
        "op",
        ServeConfig(operator="op", components={"merger": ComponentSpec(name="merger")}),
        clock=clock,
    )
    sup.children["merger"] = ChildState(
        name="merger",
        state="running",
        pid=os.getpid(),
        starttime=starttime_fingerprint(os.getpid()),
    )
    config = ServeConfig(operator="op", watchdog=WatchdogConfig(stage_timeout_minutes={"lane": 1}))
    report = Watchdog("op", config, sup, clock=clock).tick()
    assert [r for r in report.remediations if r.rule == RULE_STUCK_STAGE] == []


def test_a_stuck_stage_is_retried_once_then_escalates() -> None:
    """A reliably stuck stage must produce a readable escalation, not a loop."""
    clock = FakeClock()
    sup = _supervisor_with_child(stage_timeout_minutes={"lane": 1}, stage_retry_budget=1)
    try:
        _backdate_log(clock)
        config = _watchdog_config(stage_timeout_minutes={"lane": 1}, stage_retry_budget=1)
        watchdog = Watchdog("op", config, sup, clock=clock)
        first = watchdog.tick()
        assert [r.action for r in first.remediations if r.rule == RULE_STUCK_STAGE] == [
            "terminate_group"
        ]
        # The remediation really did terminate the child. Re-arm the ledger the
        # way a restarted component would, and backdate its last event so the
        # freshly started process is not judged on its first tick.
        time.sleep(0.2)
        sup.tick()
        sup.start("dispatcher")
        sup.children["dispatcher"].last_event_epoch = clock.time()
        _backdate_log(clock)
        second = watchdog.tick()
        assert [r.action for r in second.remediations if r.rule == RULE_STUCK_STAGE] == ["escalate"]
    finally:
        sup.shutdown()


def test_an_unowned_process_is_never_touched() -> None:
    """The safety property, stated as a test.

    A process this test starts is not in the supervisor's ledger. No rule may
    signal it, however old it is and however reparented — because on this box
    the same is true of every other operator's agents.
    """
    clock = FakeClock()
    sup = _supervisor_with_child()
    try:
        stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        try:
            report = Watchdog(
                "op",
                _watchdog_config(
                    orphan_minutes=1, stage_timeout_minutes={"lane": 1}, no_progress_minutes=1
                ),
                sup,
                clock=clock,
            ).tick(queued_depth=5)
            assert stranger.poll() is None, "a process serve never spawned must survive"
            assert all(r.pid != stranger.pid for r in report.remediations)
        finally:
            stranger.kill()
            stranger.wait(timeout=5)
    finally:
        sup.shutdown()


def test_a_dry_run_signals_nothing() -> None:
    clock = FakeClock()
    sup = _supervisor_with_child(stage_timeout_minutes={"lane": 1})
    try:
        _backdate_log(clock)
        pid = sup.children["dispatcher"].pid
        report = Watchdog(
            "op",
            _watchdog_config(stage_timeout_minutes={"lane": 1}),
            sup,
            clock=clock,
            dry_run=True,
        ).tick()
        assert [r.rule for r in report.remediations] == [RULE_STUCK_STAGE]
        from agent_fleet.serve.procs import pid_alive

        assert pid_alive(pid) is True, "a dry run must not kill anything"
    finally:
        sup.shutdown()


# ------------------------------------------------ rule (e) no-progress restarts


def test_no_progress_is_inert_when_nothing_is_queued() -> None:
    """An idle fleet with no queue is not a wedged fleet."""
    clock = FakeClock()
    sup = _supervisor_with_child()
    try:
        clock.advance(100_000)
        report = Watchdog("op", _watchdog_config(no_progress_minutes=1), sup, clock=clock).tick(
            queued_depth=0
        )
        assert [r for r in report.remediations if r.rule == RULE_NO_PROGRESS] == []
    finally:
        sup.shutdown()


def test_no_progress_restarts_a_wedged_component_with_work_waiting() -> None:
    clock = FakeClock()
    sup = _supervisor_with_child()
    try:
        first = sup.children["dispatcher"].pid
        clock.advance(100_000)
        report = Watchdog("op", _watchdog_config(no_progress_minutes=1), sup, clock=clock).tick(
            queued_depth=3
        )
        restarts = [r for r in report.remediations if r.rule == RULE_NO_PROGRESS]
        assert restarts and restarts[0].action == "restart_component"
        assert sup.children["dispatcher"].pid != first
    finally:
        sup.shutdown()


def test_no_progress_escalates_once_its_own_budget_is_spent() -> None:
    clock = FakeClock()
    sup = _supervisor_with_child()
    try:
        watchdog = Watchdog(
            "op",
            _watchdog_config(
                no_progress_minutes=1,
                no_progress_restarts=1,
                no_progress_window_minutes=1,
            ),
            sup,
            clock=clock,
        )
        clock.advance(100_000)
        first = watchdog.tick(queued_depth=3)
        assert [r.action for r in first.remediations if r.rule == RULE_NO_PROGRESS] == [
            "restart_component"
        ]
        clock.advance(100_000)
        second = watchdog.tick(queued_depth=3)
        assert [r.action for r in second.remediations if r.rule == RULE_NO_PROGRESS] == ["escalate"]
    finally:
        sup.shutdown()


# ------------------------------------------------------------------ budgeting


def test_the_per_tick_budget_defers_rather_than_silently_dropping(tmp_path: Path) -> None:
    """Silence must never be mistaken for "nothing was wrong"."""
    proc_root = _dead_proc_root(tmp_path)
    sup = Supervisor("op", ServeConfig(operator="op"), clock=FakeClock(), proc_root=proc_root)
    registry = LockRegistry("op", proc_root=proc_root)
    for i in range(6):
        registry.mark_held(f"lock-{i}", holder="merger", pid=DEAD_PID, starttime=1, now=0.0)
    config = _watchdog_config(stale_lock_minutes=1, max_remediations_per_tick=2)
    report = Watchdog(
        "op", config, sup, clock=FakeClock(), proc_root=proc_root, locks=registry
    ).tick()
    assert len(report.remediations) == 2
    assert len(report.deferred) == 4
    assert report.by_rule() == {RULE_STALE_LOCK: 2}


def test_a_watchdog_report_serialises_its_remediations() -> None:
    report = WatchdogReport()
    from agent_fleet.serve.watchdog import Remediation

    report.remediations.append(
        Remediation(rule=RULE_ORPHAN, subject="s", action="terminate_group", reason="r", pid=7)
    )
    assert report.remediations[0].to_dict() == {
        "rule": RULE_ORPHAN,
        "subject": "s",
        "action": "terminate_group",
        "reason": "r",
        "signalled": False,
        "pid": 7,
    }
    assert report.acted is True


def test_ensure_serve_dir_creates_the_layout() -> None:
    root = ensure_serve_dir("op")
    assert (root / "components").is_dir()
    assert (root / "locks").is_dir()


def test_write_json_atomic_replaces_in_place(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    write_json_atomic(path, {"a": 1})
    write_json_atomic(path, {"a": 2})
    assert path.read_text(encoding="utf-8").count('"a"') == 1


def test_component_helpers_map_to_stage_timeouts() -> None:
    assert WatchdogConfig().timeout_for("gate") == 90
    assert WatchdogConfig().timeout_for("unknown-stage") == 120
    custom = WatchdogConfig(stage_timeout_minutes={"gate": 5})
    assert custom.timeout_for("gate") == 5


def _backdate_log(clock: FakeClock) -> Path:
    """A component log whose mtime is an hour old, so the stage reads as stuck."""
    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("old", encoding="utf-8")
    os.utime(log, (clock.time() - 3600, clock.time() - 3600))
    return log
