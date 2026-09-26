"""A group TERM must be escalated with a group KILL, not a bare-pid KILL.

Two rules in this package only make sense as a matched pair:

* children are spawned with ``start_new_session=True``, so a component is its own
  process-group leader and its group contains nothing serve did not put there —
  which is what makes it safe to signal the *group*;
* ``terminate_group`` therefore exists, and every rule in ``Watchdog`` that acts
  on a component pid uses it, so a component that spawned its own workers takes
  them with it instead of leaving them running against a serve that has gone away
  (``Supervisor.shutdown``'s docstring states this as the intent).

The escalation half has to match. ``Watchdog.escalate_pending_groups`` exists and
does the group KILL — but ``Watchdog.tick`` calls plain ``escalate_pending``,
which routes to ``escalate_kill`` and does ``os.kill(pid, SIGKILL)`` on the bare
pid. ``ServeLoop._collect_survivors`` feeds it only identities that took a group
TERM, so the mismatch is not benign: the one escalation the serve loop can reach
is the one that does the wrong thing, and ``escalate_pending_groups`` has no
caller in the package at all.

The consequence is precise. A worker that ignores SIGTERM — which is the entire
reason a TERM needs escalating — is not in the supervisor's ledger, so only a
group signal can reach it. After a bare-pid KILL the leader is gone and the
worker is orphaned: still running, still holding whatever worktree or gate slot
it held, now with no supervisor that will ever signal it again. The escalation
still reports ``serve.watchdog.kill_escalated``, so the log says the survivor
was handled.

Liveness is asserted with ``pid_alive`` (a ``kill(pid, 0)`` on a non-zombie),
not ``waitpid``: a killed group leader's child is reparented to init and is not
reapable by this process, so a wait-based check would be reporting on the wrong
thing.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.procs import pid_alive
from agent_fleet.serve.serve import ServeLoop
from agent_fleet.serve.supervisor import ChildState, Supervisor
from agent_fleet.serve.watchdog import Remediation, Watchdog, WatchdogReport

if TYPE_CHECKING:
    from collections.abc import Iterator

#: A group leader that spawns a worker and both ignore SIGTERM. Written to a file
#: rather than inlined in the command template: serve execs its argv through
#: ``shlex.split``, and a nested-quote python one-liner does not survive that.
#: The worker is what only a group KILL can reach.
_LEADER_SRC = """\
import signal, subprocess, sys, time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.Popen([
    sys.executable,
    "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
])
time.sleep(300)
"""

#: How long to let a real signal land. Generous: the thing that has to fail is a
#: survivor still being alive, not a race a busy box could win.
SETTLE_S = 1.0

#: How long to let the component spawn its worker before giving up.
SPAWN_TIMEOUT_S = 15.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def _config(command: str) -> ServeConfig:
    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        components={"dispatcher": ComponentSpec(name="dispatcher", command=command)},
        watchdog=WatchdogConfig(
            stage_timeout_minutes={"lane": 1},
            stage_retry_budget=1,
        ),
    )


def _backdate_log(clock: FakeClock) -> None:
    """Make the component's tracked output read as stale, so the rule fires."""
    from agent_fleet.serve.paths import component_log_path

    log = component_log_path("op", "dispatcher")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("old", encoding="utf-8")
    os.utime(log, (clock.time() - 3600, clock.time() - 3600))


def _group_members(pgid: int) -> list[int]:
    """Every live pid in *pgid*, read from /proc/<pid>/stat field 5."""
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = entry.joinpath("stat").read_text(encoding="utf-8", errors="replace")
            close = stat.rfind(")")
            member_pgid = int(stat[close + 2 :].split()[2])
        except OSError, IndexError, ValueError:
            continue
        if member_pgid == pgid:
            members.append(int(entry.name))
    return members


def _wait_for_member(pgid: int, *, exclude: set[int], timeout: float) -> int:
    """Bounded poll for a process to appear in *pgid*. Returns its pid, or -1."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pid in _group_members(pgid):
            if pid not in exclude:
                return pid
        time.sleep(0.02)
    return -1


def _leader_script(tmp_path: Path) -> str:
    path = tmp_path / "component.py"
    path.write_text(_LEADER_SRC, encoding="utf-8")
    return f"{sys.executable} {path}"


@pytest.fixture
def group_component(tmp_path: Path) -> Iterator[tuple[Supervisor, int, int]]:
    """A running component, the worker it spawned, and a teardown scoped to the
    two pids this test recorded.

    Nothing here matches processes by name or pattern — the pids are the ones
    recorded at start time, which is the same discipline the package under test
    is built on.
    """
    clock = FakeClock()
    sup = Supervisor("op", _config(_leader_script(tmp_path)), clock=clock)
    assert sup.start("dispatcher") is True
    leader = sup.children["dispatcher"].pid
    assert leader is not None
    pgid = os.getpgid(leader)
    worker = _wait_for_member(pgid, exclude={leader, os.getpid()}, timeout=SPAWN_TIMEOUT_S)
    assert worker != -1, "the component never spawned its worker; this proves nothing"
    try:
        yield sup, leader, worker
    finally:
        for pid in (leader, worker):
            with suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        sup.shutdown()


def test_collect_survivors_queues_the_group_term_identity_not_a_bare_one(
    tmp_path: Path,  # noqa: ARG001
) -> None:
    """The identity the escalation is handed is the one that took a group TERM.

    This is the link the whole chain rests on, and it is worth pinning on its own:
    ``_collect_survivors`` queues only remediations whose action is
    ``terminate_group``, so the pid the next tick escalates is always a group
    leader — and escalating a group leader with a bare-pid SIGKILL is the defect.
    """
    loop = ServeLoop(
        operator="op",
        config=_config("true"),
        clock=FakeClock(),
        cgroup_root=Path("/nonexistent-cgroup-root"),
        max_ticks=1,
    )
    loop.supervisor.children["dispatcher"] = ChildState(
        name="dispatcher", state="running", pid=4242, starttime=77
    )

    report = WatchdogReport()
    report.remediations.append(
        Remediation(
            rule="stuck_stage", subject="dispatcher", action="terminate_group", reason="r", pid=4242
        )
    )
    # Anything the loop did not TERM as a group is not pending an escalation.
    report.remediations.append(
        Remediation(
            rule="no_progress", subject="merger", action="restart_component", reason="r", pid=5150
        )
    )

    survivors = loop._collect_survivors(report)

    assert [i.pid for i in survivors] == [4242], (
        "the escalation must only ever be handed the identity of a component that took a group TERM"
    )
    assert survivors[0].starttime == 77, "and it must be the full identity, not a bare pid"


def test_the_watchdog_escalates_a_group_term_with_a_group_kill(
    group_component: tuple[Supervisor, int, int],
) -> None:
    """The escalation must reach the worker, and only a group KILL can.

    The survivor has already taken the group TERM (the stuck-stage rule fires on
    a stale log), ignored it — the only case an escalation exists for — and is
    now pending. ``escalate_pending`` is the exact call ``Watchdog.tick`` makes
    with the list ``ServeLoop`` builds.
    """
    sup, leader, worker = group_component
    clock = FakeClock()
    watchdog = Watchdog("op", sup.config, sup, clock=clock)

    _backdate_log(clock)
    report = watchdog.tick(pending_kills=[])
    group_terms = [r for r in report.remediations if r.action == "terminate_group"]
    assert group_terms, "the stuck-stage rule must have issued a group TERM"
    assert group_terms[0].pid == leader

    # The TERM went to the group and the worker ignored it, so it is a survivor.
    time.sleep(SETTLE_S)
    assert pid_alive(worker), (
        "the worker should have survived the group TERM (it ignores SIGTERM); if it "
        "did not, this test cannot tell a group KILL apart from a bare-pid KILL"
    )

    # Exactly what ServeLoop._collect_survivors hands the next tick.
    identity = sup.children["dispatcher"].identity
    assert identity is not None and identity.pid == leader

    watchdog.escalate_pending([identity])
    time.sleep(SETTLE_S)

    assert not pid_alive(worker), (
        f"worker {worker} survived escalate_pending after taking a group TERM: the "
        "escalation SIGKILLed the bare leader pid (escalate_kill) instead of the "
        "process group (escalate_kill_group / escalate_pending_groups). A worker that "
        "ignores SIGTERM is then orphaned — still running, still holding its worktree "
        "or gate slot — and the escalation is still logged as "
        "serve.watchdog.kill_escalated, so the report claims the survivor was handled."
    )
    assert not pid_alive(leader), "the leader itself should be gone either way"
