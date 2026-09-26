"""The serve loop: one tick of capacity, watchdog, board and events.

This is the process the operator starts and then stops thinking about. Each
tick is deliberately ordered so that anything a human would want to know about
happens in the order it happened:

1. **reap and restart** components that exited, before anything reads their
   state — otherwise a dead dispatcher is reported as running for one tick;
2. **read pressure** once and reuse it for both the capacity decision and the
   status screen, so the two can never disagree;
3. **publish** the capacity targets, because a component that is about to be
   restarted needs the fresh numbers in its command template;
4. **watchdog**, on its own cadence;
5. **record** the tick, so a supervisor that is restarted mid-flight resumes
   with an accurate crash budget rather than a blank one.

The lock is taken for the process's lifetime and released last. Everything else
— pid file, children, state — is cleaned up in a ``finally``, so a SIGTERM
leaves no stale pid file for the next serve to try to adopt.

One deliberate non-goal: serve does not *become* the components. It supervises
them, sizes them, watches them and reports on them, and it does so through
command templates so it works with whatever those components happen to be — the
in-repo ``fleet dispatch`` and ``fleet merge run``, or the bash drivers they
replace. That is why the capacity file exists as a documented contract rather
than an in-process call: the consumers are separate programs.
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.serve.capacity import CapacityController, read_capacity, write_capacity
from agent_fleet.serve.clock import SystemClock
from agent_fleet.serve.events import alert, emit_serve_event
from agent_fleet.serve.items import ItemBoard
from agent_fleet.serve.paths import (
    capacity_path,
    ensure_serve_dir,
    exclusive_lock,
    items_path,
    lock_path,
    pid_path,
    write_json_atomic,
)
from agent_fleet.serve.pressure import read_pressure
from agent_fleet.serve.procs import starttime_fingerprint
from agent_fleet.serve.supervisor import Supervisor, install_pdeathsig
from agent_fleet.serve.watchdog import Watchdog

if TYPE_CHECKING:
    from agent_fleet.serve.clock import Clock
    from agent_fleet.serve.config import ServeConfig
    from agent_fleet.serve.procs import ProcIdentity
    from agent_fleet.serve.watchdog import WatchdogReport


@dataclass
class TickResult:
    """What one tick did. Returned so a test can assert on it directly."""

    index: int
    capacity_published: bool = False
    degraded: bool = False
    watchdog_remediations: int = 0
    watchdog_deferred: int = 0
    queued_depth: int = 0
    completed: int = 0
    note: str = ""


@dataclass
class ServeLoop:
    """The long-running supervisor. Construct, :meth:`run`."""

    operator: str
    config: ServeConfig
    clock: Clock = field(default_factory=SystemClock)
    cgroup_root: Path = Path("/sys/fs/cgroup")
    max_ticks: int | None = None
    dry_run_watchdog: bool = False

    def __post_init__(self) -> None:
        ensure_serve_dir(self.operator)
        self.supervisor = Supervisor(self.operator, self.config, clock=self.clock)
        self.board = ItemBoard(items_path(self.operator), clock=self.clock)
        self.controller = CapacityController(self.config.capacity, clock=self.clock)
        self._resume_capacity()
        self.watchdog = Watchdog(
            self.operator,
            self.config,
            self.supervisor,
            clock=self.clock,
            dry_run=self.dry_run_watchdog,
        )
        self._tick_index = 0
        #: Identities TERMed on a previous tick and awaiting the KILL escalation.
        self._pending_kills: list[ProcIdentity] = []

    # ------------------------------------------------------------------ setup

    def _resume_capacity(self) -> None:
        """Adopt the previous supervisor's ramp instead of restarting at floor.

        Without this, every serve restart would collapse capacity to the floor
        and spend the next several minutes climbing back — the fleet would look
        like it had recovered from an incident each time the supervisor bounced.
        """
        document = read_capacity(capacity_path(self.operator))
        if document is not None:
            self.controller.restore(document)

    def _claim_pidfile(self) -> dict[str, Any]:
        """Record this supervisor's identity. Advisory; the flock is the truth."""
        payload = {
            "operator": self.operator,
            "pid": os.getpid(),
            "starttime": starttime_fingerprint(os.getpid()),
            "started_epoch": self.clock.time(),
        }
        write_json_atomic(pid_path(self.operator), payload)
        return payload

    def _clear_pidfile(self) -> None:
        with suppress(OSError):
            pid_path(self.operator).unlink(missing_ok=True)

    # ------------------------------------------------------------------- tick

    def tick(self) -> TickResult:
        """One pass. Safe to call directly — this is what ``run`` loops over."""
        self._tick_index += 1
        result = TickResult(index=self._tick_index)

        # 1. Reap and restart first: everything below reads component state.
        self.supervisor.tick()
        self.supervisor.save()

        # 2. One pressure read, shared by the controller and the status screen.
        reading = read_pressure(self.config.cgroup, root=self.cgroup_root)
        result.degraded = not reading.ok

        # 3. Publish targets before any restart can interpolate them.
        completed = self._completions_since_last_tick()
        targets = self.controller.tick(reading, completed=completed)
        write_capacity(
            self.operator,
            targets,
            reading,
            clock=self.clock,
            idle_ticks=self.controller.idle_ticks,
            saturated=self.controller.saturated,
        )
        result.capacity_published = True
        result.completed = completed

        if reading.ok and self.controller.degraded_ticks > 0:
            emit_serve_event(
                self.operator,
                "serve.pressure.recovered",
                data={"cgroup": reading.path},
            )

        # 4. Watchdog, on its own cadence.
        depths = self.board.depth()
        queued_depth = depths.get("queued", 0) + depths.get("approved", 0)
        result.queued_depth = queued_depth
        if self._tick_index % max(1, self.config.watchdog_every_ticks) == 0:
            report: WatchdogReport = self.watchdog.tick(
                queued_depth=queued_depth,
                pending_kills=self._pending_kills,
            )
            self._pending_kills = self._collect_survivors(report)
            result.watchdog_remediations = len(report.remediations)
            result.watchdog_deferred = len(report.deferred)

        emit_serve_event(
            self.operator,
            "serve.tick",
            data={
                "tick": self._tick_index,
                "max_lanes": targets.max_lanes,
                "max_gates": targets.max_gates,
                "signal": targets.signal,
                "degraded": targets.degraded,
                "gates_priority": targets.gates_priority,
                "queued": queued_depth,
                "completed": completed,
                "remediations": result.watchdog_remediations,
            },
        )
        return result

    def _completions_since_last_tick(self) -> int:
        """Terminal transitions newer than the last tick.

        The starvation guard's input. Counted from the board's own log rather
        than from a running total so a supervisor restart does not lose it.
        """
        now = self.clock.time()
        window = max(1.0, self.config.tick_seconds) * 2.0
        return sum(
            1
            for t in self.board.transitions()
            if t.stage in ("merged", "escalated") and now - t.epoch <= window
        )

    def _collect_survivors(self, report: WatchdogReport) -> list[ProcIdentity]:
        """Identities that took a TERM and are still around; KILL them next tick.

        Deferring the escalation by one tick is what keeps the watchdog from
        sleeping inside its own evaluation.
        """
        if self.dry_run_watchdog or not report.remediations:
            return []
        survivors: list[ProcIdentity] = []
        for remediation in report.remediations:
            if remediation.action != "terminate_group" or remediation.pid is None:
                continue
            state = self.supervisor.children.get(remediation.subject)
            if state is not None and state.identity is not None:
                survivors.append(state.identity)
        return survivors

    # -------------------------------------------------------------------- run

    def run(self) -> int:
        """Hold the lock and tick until stopped. Returns a process exit code.

        The lock is the reason two supervisors for one operator cannot coexist,
        and it is taken *before* anything is spawned — a second ``fleet serve``
        exits non-zero with a message naming the pid that holds it, rather than
        racing the first one into a double dispatch.
        """
        install_pdeathsig()
        ensure_serve_dir(self.operator)
        with exclusive_lock(lock_path(self.operator)) as acquired:
            if not acquired:
                existing = _read_holder(self.operator)
                alert(
                    self.operator,
                    "serve.already_running",
                    f"another fleet serve holds the supervisor lock for {self.operator!r}",
                    holder_pid=existing,
                )
                print(
                    f"fleet serve is already running for operator {self.operator!r} "
                    f"(pid {existing or 'unknown'}). Not starting a second supervisor."
                )
                return 3

            self._claim_pidfile()
            self.supervisor.reaper_signals()
            emit_serve_event(
                self.operator,
                "serve.started",
                data={
                    "pid": os.getpid(),
                    "components": [c.name for c in self.config.enabled_components],
                    "cgroup": self.config.cgroup,
                    "tick_seconds": self.config.tick_seconds,
                },
            )
            try:
                return self._loop()
            finally:
                self.supervisor.shutdown()
                self.supervisor.save()
                self._clear_pidfile()
                emit_serve_event(self.operator, "serve.stopped", data={"ticks": self._tick_index})

    def _loop(self) -> int:
        while True:
            if self.max_ticks is not None and self._tick_index >= self.max_ticks:
                return 0
            self.tick()
            if self.max_ticks is not None:
                continue
            self.clock.sleep(self.config.tick_seconds)

    # ------------------------------------------------------------------- stop

    def stop(self) -> bool:
        """Stop a running supervisor, by the pid it recorded.

        Refuses to signal anything whose fingerprint does not match the recorded
        one, so a recycled pid belonging to an unrelated process is left alone.
        """
        from agent_fleet.serve.paths import read_json
        from agent_fleet.serve.procs import ProcIdentity, terminate

        payload = read_json(pid_path(self.operator))
        if not payload:
            return False
        pid = payload.get("pid")
        starttime = payload.get("starttime")
        if not isinstance(pid, int) or not isinstance(starttime, int):
            return False
        result = terminate(ProcIdentity(pid=pid, starttime=starttime))
        return result.signalled


def _read_holder(operator: str) -> int | None:
    from agent_fleet.serve.paths import read_json

    payload = read_json(pid_path(operator))
    if not payload:
        return None
    pid = payload.get("pid")
    return int(pid) if isinstance(pid, int) else None


__all__ = ["ServeLoop", "TickResult"]
