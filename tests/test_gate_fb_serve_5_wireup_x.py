"""Round 2: three confirmed defects in the serve wire-up.

Each test reproduces its report's repro steps exactly and asserts the operator-
visible consequence, not an implementation detail.

all-1 — ``agent_fleet/serve/serve.py`` (tick ordering)
    ``ServeLoop.tick`` reaped and restarted components (step 1) *before*
    ``write_capacity`` published the targets (step 3). ``Supervisor.start``
    expands a component's command template by reading the capacity file back
    through ``Supervisor._targets_for``, which is ``None`` until something has
    written one. So on a cold start every numeric placeholder rendered as an
    empty string, ``expand_command`` dropped the empty argument, and the shipped
    template turned into adjacent flags::

        --max-lanes {max_lanes} --max-gates {max_gates}
          ->  --max-lanes --max-gates

    argparse then reads ``--max-gates`` as the *value* of ``--max-lanes`` and
    exits 2, so the dispatcher spawns, dies instantly, and the exit is booked as
    a crash against the crash budget on every cold start.

all-2 — ``agent_fleet/serve/supervisor.py`` (``Supervisor.tick``)
    The crash backoff was slept synchronously inside the reap loop. The serve
    loop is one call deep, so a single backoff froze everything behind it for
    the delay's full length — with the shipped defaults (5s growing to 300s)
    capacity targets went unpublished and the watchdog never ran. The tick must
    return promptly and carry the wait as a scheduled restart.

all-3 — ``agent_fleet/serve/watchdog.py`` (``check_deadlocks``)
    Deadlock remediation called ``LockRegistry.release``, which only rewrites
    the JSON record. The flock is the authoritative mechanism, so rewriting the
    record left a *real* two-way deadlock in place while making it invisible to
    the detector: ``deadlocks()`` then returns ``[]`` and the watchdog reports a
    resolved deadlock that is still wedged. The module docstring promises "the
    flock guarantees the release is real, not a note in a file".

Every wait is a bounded poll with an exit condition. Every signal goes to a pid
this file recorded itself — never a name or a pattern.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.serve.clock import FakeClock, SystemClock
from agent_fleet.serve.config import ComponentSpec, ServeConfig, WatchdogConfig
from agent_fleet.serve.locks import STATE_FREE, LockRegistry
from agent_fleet.serve.paths import capacity_path, read_json
from agent_fleet.serve.procs import pid_alive, starttime_fingerprint
from agent_fleet.serve.serve import ServeLoop
from agent_fleet.serve.supervisor import STATE_CRASH_LOOPING, Supervisor
from agent_fleet.serve.watchdog import RULE_DEADLOCK, Watchdog, WatchdogReport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Generous enough that a loaded box is not what fails these tests.
WAIT_S = 5.0

#: The dispatcher template exactly as ``fleet.example.yaml`` ships it.
DISPATCH_TEMPLATE = (
    "fleet dispatch --operator {operator} --max-lanes {max_lanes} --max-gates {max_gates}"
)

#: A cgroup root that does not exist, so the tick reads as degraded.
NO_CGROUP = Path("/nonexistent-cgroup-root")


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


def _flock_held(path: Path) -> bool:
    """True when some process really holds an exclusive flock on *path*.

    Probed with a fresh file descriptor, which is the only honest way to ask:
    the JSON record beside the lock is metadata and can say anything.
    """
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        return False
    finally:
        os.close(handle)


# --------------------------------------------------------------------- all-1
# Capacity must be published before anything spawns a command template.


def _dispatcher_config() -> ServeConfig:
    return ServeConfig(
        operator="op",
        tick_seconds=0.01,
        components={
            "dispatcher": ComponentSpec(name="dispatcher", command=DISPATCH_TEMPLATE),
        },
    )


@pytest.fixture
def captured_argvs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every argv the supervisor would exec, recorded and replaced with a sleeper.

    ``Supervisor.start`` does not keep its argv, so the spawn is intercepted at
    the one call that produces it: ``expand_command``. The returned argv is
    recorded, and a real sleeper is substituted so the component actually runs
    and the crash budget is not polluted by a fake spawn failure. These tests
    are about what the supervisor *builds*, not about the dispatcher running.
    """
    seen: list[list[str]] = []
    real_expand = supervisor_expand()
    sleeper = f"{sys.executable} -c 'import time; time.sleep(30)'"

    def spy(template: str, **kwargs: Any) -> list[str]:  # noqa: ANN401
        argv = real_expand(template, **kwargs)
        seen.append(list(argv))
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    monkeypatch.setattr("agent_fleet.serve.supervisor.expand_command", spy)
    del sleeper
    return seen


def supervisor_expand() -> Any:  # noqa: ANN401
    from agent_fleet.serve import supervisor as supervisor_module

    return supervisor_module.expand_command


def _flag_value(argv: list[str], flag: str) -> str | None:
    """The token after *flag*, or None when the flag has no value at all.

    Written as a lookup rather than an index so a dropped argument produces a
    clean failure naming the defect instead of an IndexError: the whole bug is
    that ``--max-gates`` ends up *as* the value of ``--max-lanes``.
    """
    try:
        position = argv.index(flag)
    except ValueError:
        return None
    if position + 1 >= len(argv):
        return None
    return argv[position + 1]


def test_a_cold_start_expands_the_numeric_placeholders(
    captured_argvs: list[list[str]],
) -> None:
    """The first spawn of a fresh operator must carry real numbers.

    The whole chain in one assertion: the template carries ``{max_lanes}`` and
    ``{max_gates}``, the capacity file does not exist yet, and the spawn still
    has to produce ``--max-lanes 2 --max-gates 2`` — because the tick publishes
    the targets *before* it reaps and restarts.
    """
    loop = ServeLoop(
        operator="op",
        config=_dispatcher_config(),
        clock=FakeClock(),
        cgroup_root=NO_CGROUP,
        max_ticks=1,
    )
    try:
        # The precondition the defect depends on: nothing published yet.
        assert read_json(capacity_path("op")) is None, (
            "a fresh operator must have no capacity file, or this proves nothing"
        )

        loop.tick()

        assert read_json(capacity_path("op")) is not None, "the tick must publish targets"

        assert captured_argvs, "the dispatcher should have been spawned"
        argv = captured_argvs[0]
        assert "--max-lanes" in argv and "--max-gates" in argv, f"argv was {argv}"

        lanes = _flag_value(argv, "--max-lanes")
        gates = _flag_value(argv, "--max-gates")
        assert lanes is not None and lanes.isdigit(), (
            f"--max-lanes got {lanes!r}: the placeholder rendered as an empty string "
            "and the argument was dropped, so the flag is now reading the *next* "
            f"flag as its value — argparse will exit 2 on every cold start. argv was "
            f"{argv}"
        )
        assert gates is not None and gates.isdigit(), f"--max-gates got {gates!r}; argv was {argv}"
    finally:
        loop.supervisor.shutdown()


def test_a_cold_start_does_not_spend_the_crash_budget_on_its_own_argv() -> None:
    """The corruption showed up as an argparse exit 2, charged to the budget.

    A component whose flags are malformed exits 2 on every spawn, which the
    supervisor cannot tell from a real crash. Five cold starts therefore declare
    it crash-looping for a fault that exists only in the argv this package
    builds. With the ordering fixed the component gets a well-formed argv, so a
    dispatcher that *does* exit is exiting for its own reasons — and here it is
    a command that is not on PATH, which is one such reason, once, not forever.
    """
    loop = ServeLoop(
        operator="op",
        config=_dispatcher_config(),
        clock=FakeClock(),
        cgroup_root=NO_CGROUP,
        max_ticks=6,
    )
    try:
        for _ in range(6):
            loop.tick()
        state = loop.supervisor.children.get("dispatcher")
        assert state is not None
        assert state.state != STATE_CRASH_LOOPING, (
            "a component that only ever failed because the supervisor built it a "
            "malformed argv was declared crash-looping; the budget was spent on a "
            "defect in the spawn path rather than on the component"
        )
    finally:
        loop.supervisor.shutdown()


# --------------------------------------------------------------------- all-2
# A crash backoff must not block the serve loop.


def test_a_backoff_does_not_block_the_serve_loop() -> None:
    """A one-shot component must not turn every tick into a sleep.

    ``janitor: command: "true"`` is the shipped shape: it exits at once, so
    every tick owes it a restart and every restart owes a backoff. With the
    backoff slept inline, ``ServeLoop.tick`` took the full delay — so capacity
    publication and the watchdog behind it did not run at all for its duration.
    """
    config = ServeConfig(
        operator="op",
        tick_seconds=0.01,
        components={
            "janitor": ComponentSpec(
                name="janitor",
                command="true",
                backoff_initial_s=0.5,
                backoff_max_s=2.0,
                crash_threshold=50,
            )
        },
    )
    loop = ServeLoop(operator="op", config=config, clock=SystemClock(), cgroup_root=NO_CGROUP)
    try:
        # A real clock, so a blocking sleep is a real wall-clock cost.
        durations: list[float] = []
        for _ in range(6):
            started = time.monotonic()
            loop.tick()
            durations.append(time.monotonic() - started)

        worst = max(durations)
        assert worst < 0.25, (
            f"one ServeLoop.tick took {worst:.2f}s (per-tick: "
            f"{[round(d, 2) for d in durations]}). Supervisor.tick is sleeping the "
            "crash backoff inline, so a single failing component freezes capacity "
            "publication and the whole watchdog behind it for the delay's full "
            "length; with the shipped defaults that is up to 300s"
        )
    finally:
        loop.supervisor.shutdown()


def test_a_backoff_still_delays_the_restart() -> None:
    """Deferring the wait must not turn the backoff into a hot loop.

    The other side of the same change: the restart is still owed, still not
    immediate, and still happens once the deadline has passed.
    """
    clock = FakeClock()
    config = ServeConfig(
        operator="op",
        tick_seconds=0.01,
        components={
            "janitor": ComponentSpec(
                name="janitor",
                command="true",
                backoff_initial_s=5.0,
                backoff_max_s=30.0,
                crash_threshold=50,
            )
        },
    )
    sup = Supervisor("op", config, clock=clock)
    sup.defer_restarts = True
    try:
        assert sup.start("janitor") is True
        assert _wait_until(
            lambda: (
                sup._procs.get("janitor") is not None and sup._procs["janitor"].poll() is not None
            ),
            WAIT_S,
        ), "the one-shot component never exited"

        sup.tick()
        state = sup.children["janitor"]
        assert state.state == "backoff", "a crashed component must be backing off"
        assert state.pid is None, "it must not be restarted inside the tick that reaped it"
        assert sup.pending_restarts() > 0, "the residual wait must be accounted for"

        # Still owed: advancing the clock is what releases it.
        clock.advance(state.restart_at - clock.monotonic() + 0.01)
        sup.tick()
        assert sup.children["janitor"].pid is not None, (
            "once the backoff deadline has passed the component must be restarted"
        )
    finally:
        sup.shutdown()


# --------------------------------------------------------------------- all-3
# A deadlock remediation must break the lock, not just rewrite its record.

#: A child that takes a real exclusive flock and holds it until signalled.
_HOLDER_SRC = """\
import fcntl, os, signal, sys, time

lock = sys.argv[1]
handle = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(handle, fcntl.LOCK_EX)
sys.stdout.write("held\\n")
sys.stdout.flush()
while True:
    time.sleep(0.2)
"""


class _Holder:
    """A child holding a real flock, with a teardown scoped to its own pid.

    Started in its own session so it is a process-*group* leader, exactly as
    ``Supervisor.start`` spawns every component. That is what makes the group
    signal in ``terminate_group`` legal at all — it refuses any pid that is not
    a group leader, so a child sharing this test's session would be skipped and
    the test would prove nothing.
    """

    def __init__(self, tmp_path: Path) -> None:
        script = tmp_path / "holder.py"
        script.write_text(_HOLDER_SRC, encoding="utf-8")
        self.lock = tmp_path / "repo-x.lock"
        self.lock.touch()
        self.proc = subprocess.Popen(
            [sys.executable, str(script), str(self.lock)],
            start_new_session=True,
        )
        assert _wait_until(lambda: _flock_held(self.lock), WAIT_S), (
            "the child never took the lock; this test would prove nothing"
        )
        assert os.getpgid(self.pid) == self.pid, (
            "the holder must lead its own process group, or the group signal the "
            "deadlock rule uses would be (correctly) refused"
        )

    @property
    def pid(self) -> int:
        return self.proc.pid

    def starttime(self) -> int:
        value = starttime_fingerprint(self.pid)
        assert value is not None
        return value

    def close(self) -> None:
        # Only the pid this holder recorded.
        with suppress(OSError):
            self.proc.send_signal(signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=10)


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[_Holder]:
    h = _Holder(tmp_path)
    try:
        yield h
    finally:
        h.close()


def _two_way_cycle(registry: LockRegistry, holder_pid: int, starttime: int) -> None:
    """Write the four records a real two-way deadlock produces.

    A holds repo-x and waits for lane-7; B holds lane-7 and waits for repo-x.
    The rule releases the *oldest* claim, so ``repo-x`` — the one a real child
    really holds — is given the oldest acquisition epoch, which is also the
    natural ordering: it was taken first.
    """
    registry.mark_held("repo-x", holder="A", pid=holder_pid, starttime=starttime, now=0.0)
    registry.mark_waiting(
        "A-wait",
        holder="A",
        pid=holder_pid,
        starttime=starttime,
        waiting_for="lane-7",
        now=10.0,
    )
    registry.mark_held("lane-7", holder="B", pid=holder_pid, starttime=starttime, now=10.0)
    registry.mark_waiting(
        "B-wait",
        holder="B",
        pid=holder_pid,
        starttime=starttime,
        waiting_for="repo-x",
        now=20.0,
    )


def _run_deadlock_rule(registry: LockRegistry) -> WatchdogReport:
    config = ServeConfig(operator="op", watchdog=WatchdogConfig(deadlock_minutes=1))
    sup = Supervisor("op", config, clock=FakeClock())
    watchdog = Watchdog("op", config, sup, clock=FakeClock(), locks=registry, dry_run=False)
    report = WatchdogReport()
    watchdog.check_deadlocks(report)
    return report


def test_deadlock_remediation_breaks_the_real_flock(holder: _Holder) -> None:
    """The cycle is broken in the kernel, not just in the JSON.

    The record must end up free *and* the flock must become free when probed
    with a fresh file descriptor. Rewriting the record alone leaves the holder
    blocked, and the next ``deadlocks()`` call then reports the wedge as gone.
    """
    registry = LockRegistry("op", proc_root=Path("/proc"))
    _two_way_cycle(registry, holder.pid, holder.starttime())
    assert _flock_held(holder.lock), "the child must really be holding the lock"

    report = _run_deadlock_rule(registry)

    assert [r for r in report.remediations if r.rule == RULE_DEADLOCK], (
        "the two-way cycle is stable past the threshold and must be acted on"
    )
    released = registry.read("repo-x")
    assert released is not None and released.state == STATE_FREE, (
        "the record must be freed, or the detector re-reports the same cycle"
    )
    assert _wait_until(lambda: not _flock_held(holder.lock), WAIT_S), (
        "the flock is still held after the remediation: rewriting the record "
        "masked the deadlock from the detector without breaking it, so the "
        "watchdog reports a resolved deadlock that both holders are still stuck "
        "inside. The record is metadata; the flock is the lock."
    )


def test_deadlock_remediation_actually_stops_the_holder(holder: _Holder) -> None:
    """The holder process is gone, not just unrecorded.

    Complementary to the flock probe: the record and the kernel lock can both
    read as released while the process is still running, which is what "masked"
    looked like in practice.
    """
    registry = LockRegistry("op", proc_root=Path("/proc"))
    _two_way_cycle(registry, holder.pid, holder.starttime())

    _run_deadlock_rule(registry)

    assert _wait_until(lambda: not pid_alive(holder.pid), WAIT_S), (
        f"the recorded holder (pid {holder.pid}) is still running after a deadlock "
        "remediation: the rule released the record but never let go of the process "
        "that actually holds the lock, so the deadlock was never broken"
    )


def test_a_deadlock_break_does_not_touch_a_process_serve_did_not_record(
    holder: _Holder,
) -> None:
    """A lock held by an unrelated process must not be killed on a bad record.

    The break is by recorded identity with a start-time fingerprint, never by
    pid alone and never by name — the same discipline the rest of the package
    uses, because this box runs other agents' processes too.
    """
    registry = LockRegistry("op", proc_root=Path("/proc"))
    wrong = holder.starttime() + 999
    _two_way_cycle(registry, holder.pid, wrong)

    _run_deadlock_rule(registry)

    assert pid_alive(holder.pid), (
        f"pid {holder.pid} was signalled on a record whose start-time fingerprint "
        "does not match: a recycled or fabricated pid must never be signalled on "
        "the strength of the JSON file alone"
    )
    assert _flock_held(holder.lock), "the unrelated holder must still hold its lock"
