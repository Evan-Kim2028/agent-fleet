"""Self-healing: five detectors, five remediations, one budget.

Every rule is the same shape — a pure detector that decides, and a remediation
that acts — and every remediation obeys three constraints:

**Only fleet-owned processes.** A pid must appear in the ledger serve wrote
when it spawned the process, and its start-time fingerprint must still match.
There is no name matching, no pattern matching, and no scanning of processes
serve did not start. This is the constraint that makes the rest safe: the
machine runs other agents, several of which run the same engines with lane
names in their argv, so a pattern-based kill is a coin flip with someone else's
work on it.

**Fail closed, then let the owner retry.** A stuck stage is killed and marked
dead rather than left running. "Dead" means the owning component may retry it
once (budget in config) and then must escalate — a stage that is reliably stuck
should produce an escalation a human can read, not an infinite kill/retry loop
that burns the fleet's budget on one lane.

**One budget per tick.** Remediation is capped per tick
(``max_remediations_per_tick``), and the grace before escalating TERM to KILL
is spent once for the whole tick rather than once per victim. Without this a
crash that left twenty stale children would make a single watchdog tick take
minutes, the watchdog would fall behind, and it would trip its own
no-progress rule — the watchdog creating the condition it exists to detect.

The five rules, and the failure each one was written for:

``stuck_stage``
    An agent produced no output growth for longer than its stage timeout. The
    bash watchdog compared ``stat -c %Y`` on a run's jsonl against a flat 600s;
    this compares the same signal against a per-stage timeout, so a long gate
    is not mistaken for a wedged one.

``orphan_blocking``
    A child serve spawned is still alive, its parent is gone, and it is older
    than the orphan window — a ``tail -f`` or a watcher that outlived the agent
    driving it. Scoped to serve's own ledger on purpose: the legacy detector
    found these by scanning ``ps`` for an engine name, which on a shared box
    finds other operators' processes too.

``stale_lock``
    A lock record says held, but the holder's pid is gone and the record is
    older than the grace. The flock itself is already free — the kernel
    released it — so the record is the stale part, and releasing it is what
    makes the lock visible in ``serve status`` again.

``deadlock``
    Two components each hold what the other wants, and the cycle has been
    stable past the threshold. The older claim is released and recorded; the
    flock guarantees the release is real, not a note in a file.

``no_progress``
    A component has queued work and has emitted nothing for the window. The
    restart is tagged ``requested`` so it does not consume the crash budget —
    a component that keeps needing this rule is a problem, but it is not
    crash-looping, and conflating the two would stop restarting it for real.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from agent_fleet.serve.events import alert, emit_serve_event
from agent_fleet.serve.locks import LockRegistry
from agent_fleet.serve.procs import (
    ProcIdentity,
    boot_time,
    escalate_kill,
    escalate_kill_group,
    parent_pid,
    terminate_group,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.serve.clock import Clock
    from agent_fleet.serve.config import ServeConfig
    from agent_fleet.serve.supervisor import Supervisor

RULE_STUCK_STAGE = "stuck_stage"
RULE_ORPHAN = "orphan_blocking"
RULE_STALE_LOCK = "stale_lock"
RULE_DEADLOCK = "deadlock"
RULE_NO_PROGRESS = "no_progress"

ALL_RULES = (RULE_STUCK_STAGE, RULE_ORPHAN, RULE_STALE_LOCK, RULE_DEADLOCK, RULE_NO_PROGRESS)


class _StatLike(Protocol):
    st_mtime: float


@dataclass(frozen=True)
class Remediation:
    """One action taken, and the record of why."""

    rule: str
    subject: str
    action: str
    reason: str
    signalled: bool = False
    pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "subject": self.subject,
            "action": self.action,
            "reason": self.reason,
            "signalled": self.signalled,
            "pid": self.pid,
        }


@dataclass
class WatchdogReport:
    """What one watchdog tick found and did."""

    remediations: list[Remediation] = field(default_factory=list)
    #: Findings the budget did not allow, reported so silence is never mistaken
    #: for "nothing was wrong".
    deferred: list[Remediation] = field(default_factory=list)
    dry_run: bool = False

    @property
    def acted(self) -> bool:
        return bool(self.remediations)

    def by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.remediations:
            counts[item.rule] = counts.get(item.rule, 0) + 1
        return counts


def growth_idle_seconds(
    path: Path,
    *,
    now: float,
    stat: _StatLike | None = None,
) -> float:
    """Seconds since *path* last grew, or ``inf`` when it is missing.

    ``stat`` is injected so the rule is testable without touching the
    filesystem's idea of "now". A missing output file is ``inf`` rather than
    ``0.0``: a stage that has produced nothing at all is maximally stuck, and
    returning zero would exempt exactly the worst case.
    """
    if stat is None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return float("inf")
    else:
        mtime = stat.st_mtime
    return max(0.0, now - mtime)


class Watchdog:
    """Runs the five rules, in order of blast radius: cheap checks first."""

    def __init__(
        self,
        operator: str,
        config: ServeConfig,
        supervisor: Supervisor,
        *,
        clock: Clock | None = None,
        proc_root: Path | None = None,
        locks: LockRegistry | None = None,
        dry_run: bool = False,
    ) -> None:
        self.operator = operator
        self.config = config
        self.supervisor = supervisor
        self.clock = clock or supervisor.clock
        self.proc_root = proc_root or supervisor.proc_root
        self.locks = locks or LockRegistry(operator, proc_root=self.proc_root)
        self.dry_run = dry_run
        #: stage -> retries already spent, enforcing ``stage_retry_budget``.
        self.stage_retries: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers

    def _budget_left(self, report: WatchdogReport) -> bool:
        return len(report.remediations) < self.config.watchdog.max_remediations_per_tick

    def _record(
        self,
        report: WatchdogReport,
        remediation: Remediation,
        event: str,
        *,
        level: str = "warning",
        **data: Any,  # noqa: ANN401
    ) -> None:
        if self.dry_run:
            report.remediations.append(remediation)
            return
        report.remediations.append(remediation)
        emit_serve_event(
            self.operator,
            event,
            level=level,
            data={**remediation.to_dict(), **data},
        )

    def _defer(self, report: WatchdogReport, remediation: Remediation) -> None:
        report.deferred.append(remediation)

    # ------------------------------------------------------- rule (c) stale lock

    def check_stale_locks(self, report: WatchdogReport) -> None:
        """Locks whose holder is gone, older than the grace.

        Purely a record problem: the kernel already dropped the flock when the
        holder died, so nothing is actually blocked. Releasing the record is
        what makes the lock show as free again and stops a later deadlock
        detector from reading a phantom edge.
        """
        now = self.clock.time()
        for record in self.locks.stale_locks(
            now=now, grace_minutes=self.config.watchdog.stale_lock_minutes
        ):
            remediation = Remediation(
                rule=RULE_STALE_LOCK,
                subject=record.name,
                action="release_lock_record",
                reason=(
                    f"holder {record.holder or 'unknown'} (pid {record.pid}) is gone; "
                    f"record held for {record.age_s(now) / 60:.0f}m "
                    f"(grace {self.config.watchdog.stale_lock_minutes}m)"
                ),
                pid=record.pid,
            )
            if not self._budget_left(report):
                self._defer(report, remediation)
                continue
            if not self.dry_run:
                self.locks.release(record.name, note=f"watchdog: {remediation.reason}")
            self._record(
                report,
                remediation,
                "serve.watchdog.stale_lock",
                component=record.holder,
            )

    # -------------------------------------------------------- rule (d) deadlock

    def check_deadlocks(self, report: WatchdogReport) -> None:
        """Cycles of waiting, stable past the threshold.

        The older claim in the cycle is released. Age is the tiebreak because
        in a two-way deadlock neither party is more wrong; the one that has been
        waiting longer is the one whose owner has most likely already given up,
        and releasing its claim is what lets the other side finish.
        """
        now = self.clock.time()
        for cycle in self.locks.deadlocks(
            now=now, threshold_minutes=self.config.watchdog.deadlock_minutes
        ):
            names = " -> ".join(r.name for r in cycle)
            oldest = max(cycle, key=lambda r: r.age_s(now))
            remediation = Remediation(
                rule=RULE_DEADLOCK,
                subject=oldest.name,
                action="release_lock_record",
                reason=(
                    f"deadlock cycle ({names}); releasing the oldest claim "
                    f"({oldest.holder or 'unknown'}, held {oldest.age_s(now) / 60:.0f}m)"
                ),
                pid=oldest.pid,
            )
            if not self._budget_left(report):
                self._defer(report, remediation)
                continue
            if not self.dry_run:
                self.locks.release(oldest.name, note=f"watchdog deadlock: {names}")
            self._record(
                report,
                remediation,
                "serve.watchdog.deadlock",
                component=oldest.holder,
                cycle=[r.name for r in cycle],
            )

    # ------------------------------------------------------ rule (b) orphan block

    def check_orphans(self, report: WatchdogReport) -> None:
        """Children serve spawned that outlived their parent and aged out.

        Scoped to the supervisor's own children, by recorded fingerprint. A
        process serve did not spawn is invisible here by construction — the
        legacy detector found orphans by scanning ``ps`` for an engine name,
        and on a box where several operators run the same engine that scan
        matches other people's processes.
        """
        now = self.clock.time()
        window_s = max(1.0, float(self.config.watchdog.orphan_minutes) * 60.0)
        self_pid = self.supervisor_pid()

        for name, state in list(self.supervisor.children.items()):
            identity = state.identity
            if identity is None or not identity.matches(proc_root=self.proc_root):
                continue
            ppid = parent_pid(identity.pid, proc_root=self.proc_root)
            reparented = ppid is None or ppid == 1 or ppid == self_pid
            if not reparented:
                continue
            started = boot_time(identity.pid, proc_root=self.proc_root)
            age_s = (now - started) if started is not None else 0.0
            if age_s < window_s:
                continue
            remediation = Remediation(
                rule=RULE_ORPHAN,
                subject=name,
                action="terminate_group",
                reason=(
                    f"child pid {identity.pid} has been orphaned "
                    f"(parent {ppid if ppid is not None else 'gone'}) for "
                    f"{age_s / 60:.0f}m (window {self.config.watchdog.orphan_minutes}m)"
                ),
                pid=identity.pid,
            )
            if not self._budget_left(report):
                self._defer(report, remediation)
                continue
            if self.dry_run:
                self._record(report, remediation, "serve.watchdog.orphan", dry_run=True)
                continue
            term = terminate_group(identity, proc_root=self.proc_root)
            self._record(
                report,
                replace(remediation, signalled=term.signalled),
                "serve.watchdog.orphan",
                component=name,
                skipped=term.skipped_reason,
            )

    def supervisor_pid(self) -> int | None:
        state = self.supervisor.children.get("supervisor")
        return state.pid if state else None

    # ------------------------------------------------------ rule (a) stuck stage

    def check_stuck_stages(self, report: WatchdogReport) -> None:
        """Stages whose tracked output file has not grown.

        A stage is only eligible if serve knows a live process *and* a tracked
        output file for it — the recorded pid, the fingerprint and the file
        have to agree, because any one of them alone can point at a process
        that has nothing to do with this stage.
        """
        now = self.clock.time()
        for name, state in list(self.supervisor.children.items()):
            identity = state.identity
            if identity is None or not identity.matches(proc_root=self.proc_root):
                continue
            output = self._tracked_output(name)
            if output is None:
                continue
            stage = self._stage_for(name)
            timeout_minutes = self.config.watchdog.timeout_for(stage)
            idle = growth_idle_seconds(output, now=now)
            if idle < timeout_minutes * 60.0:
                continue
            spent = self.stage_retries.get(name, 0)
            if spent >= self.config.watchdog.stage_retry_budget:
                remediation = Remediation(
                    rule=RULE_STUCK_STAGE,
                    subject=name,
                    action="escalate",
                    reason=(
                        f"no output growth for {idle / 60:.0f}m (stage {stage} timeout "
                        f"{timeout_minutes}m) and the retry budget is spent"
                    ),
                    pid=identity.pid,
                )
                if not self._budget_left(report):
                    self._defer(report, remediation)
                    continue
                alert(
                    self.operator,
                    "serve.watchdog.stage_dead",
                    remediation.reason,
                    component=name,
                    stage=stage,
                )
                self._record(report, remediation, "serve.watchdog.stage_dead")
                continue
            remediation = Remediation(
                rule=RULE_STUCK_STAGE,
                subject=name,
                action="terminate_group",
                reason=(
                    f"no output growth for {idle / 60:.0f}m (stage {stage} timeout "
                    f"{timeout_minutes}m); retry {spent + 1} of "
                    f"{self.config.watchdog.stage_retry_budget}"
                ),
                pid=identity.pid,
            )
            if not self._budget_left(report):
                self._defer(report, remediation)
                continue
            if self.dry_run:
                self._record(report, remediation, "serve.watchdog.stuck_stage", dry_run=True)
                continue
            self.stage_retries[name] = spent + 1
            term = terminate_group(identity, proc_root=self.proc_root)
            self._record(
                report,
                replace(remediation, signalled=term.signalled),
                "serve.watchdog.stuck_stage",
                component=name,
                stage=stage,
                skipped=term.skipped_reason,
            )

    def _tracked_output(self, component: str) -> Path | None:
        """The file whose growth proves this component is doing work.

        A component's log is the tracked output: it is the one file serve
        guarantees exists and the one a wedged component stops writing. A
        component with no log is skipped rather than assumed stuck — a detector
        that fires on missing evidence is a detector that fires on everything.
        """
        from agent_fleet.serve.paths import component_log_path

        path = component_log_path(self.operator, component)
        return path if path.exists() else None

    def _stage_for(self, component: str) -> str:
        """Map a component role to the stage-timeout bucket it answers to."""
        return {
            "dispatcher": "lane",
            "merger": "merge",
            "janitor": "lane",
        }.get(component, "lane")

    # -------------------------------------------------- rule (e) no progress

    def check_no_progress(self, report: WatchdogReport, *, queued_depth: int = 0) -> None:
        """Components with work waiting and nothing said for the window.

        Restarts are budgeted separately from crashes *and* from each other: a
        component restarted for this rule twice inside its window is a component
        that needs a human, so the third finding escalates instead of looping.
        """
        if queued_depth <= 0:
            return
        now = self.clock.time()
        window_s = max(1.0, float(self.config.watchdog.no_progress_minutes) * 60.0)
        for spec in self.config.enabled_components:
            state = self.supervisor.children.get(spec.name)
            if state is None or state.pid is None:
                continue
            idle_s = now - state.last_event_epoch
            if idle_s < window_s:
                continue
            budget_window_s = max(1.0, float(spec.no_progress_window_minutes) * 60.0)
            # The budget counts a *burst* of restarts: the ones clustered around
            # the most recent one, measured backwards from it. Measuring
            # forwards from now would let restarts age out one at a time, so a
            # component that genuinely needs restarting forever would never
            # reach its budget and the rule would just churn it forever.
            recent = state.no_progress_restarts
            restarts = sum(1 for e in recent if recent[-1] - e <= budget_window_s) if recent else 0
            if restarts >= spec.no_progress_restarts:
                remediation = Remediation(
                    rule=RULE_NO_PROGRESS,
                    subject=spec.name,
                    action="escalate",
                    reason=(
                        f"no events for {idle_s / 60:.0f}m with {queued_depth} item(s) queued, "
                        f"and {restarts} restart(s) already tried "
                        f"(budget {spec.no_progress_restarts})"
                    ),
                    pid=state.pid,
                )
                if not self._budget_left(report):
                    self._defer(report, remediation)
                    continue
                alert(
                    self.operator,
                    "serve.watchdog.no_progress",
                    remediation.reason,
                    component=spec.name,
                )
                self._record(report, remediation, "serve.watchdog.no_progress")
                continue
            remediation = Remediation(
                rule=RULE_NO_PROGRESS,
                subject=spec.name,
                action="restart_component",
                reason=(
                    f"no events for {idle_s / 60:.0f}m with {queued_depth} item(s) queued; "
                    f"restart {restarts + 1} of {spec.no_progress_restarts}"
                ),
                pid=state.pid,
            )
            if not self._budget_left(report):
                self._defer(report, remediation)
                continue
            if self.dry_run:
                self._record(report, remediation, "serve.watchdog.no_progress", dry_run=True)
                continue
            self.supervisor.request_restart(spec.name, reason=remediation.reason)
            self._record(
                report,
                remediation,
                "serve.watchdog.no_progress",
                component=spec.name,
            )

    # ---------------------------------------------------------- grace escalation

    def escalate_pending(self, pending: list[ProcIdentity]) -> list[ProcIdentity]:
        """KILL whatever survived the grace from the previous tick.

        Split across ticks on purpose: TERM goes out, the tick ends, and the
        grace is spent by the *caller's* next tick. A watchdog that sleeps
        inside its own rule evaluation cannot keep up with the fleet it is
        protecting, which is how it ends up restarting components it should
        have been watching.
        """
        if self.dry_run:
            return []
        survivors: list[ProcIdentity] = []
        for identity in pending:
            result = escalate_kill(identity, proc_root=self.proc_root)
            if result.signalled:
                emit_serve_event(
                    self.operator,
                    "serve.watchdog.kill_escalated",
                    data={"pid": identity.pid, "skipped": result.skipped_reason},
                )
            if identity.matches(proc_root=self.proc_root):
                survivors.append(identity)
        return survivors

    def escalate_pending_groups(self, pending: list[ProcIdentity]) -> list[ProcIdentity]:
        survivors: list[ProcIdentity] = []
        for identity in pending:
            result = escalate_kill_group(identity, proc_root=self.proc_root)
            if result.signalled:
                emit_serve_event(
                    self.operator,
                    "serve.watchdog.kill_group_escalated",
                    data={"pid": identity.pid, "skipped": result.skipped_reason},
                )
            if identity.matches(proc_root=self.proc_root):
                survivors.append(identity)
        return survivors

    # -------------------------------------------------------------------- tick

    def tick(
        self,
        *,
        queued_depth: int = 0,
        pending_kills: list[ProcIdentity] | None = None,
    ) -> WatchdogReport:
        """One watchdog pass. Returns the report.

        Rule order is blast radius, cheapest and least dangerous first: lock
        records are pure metadata, a deadlock release is a record too, an orphan
        or a stuck stage is a signal, and a component restart is the only one
        that changes what the fleet is doing.
        """
        report = WatchdogReport(dry_run=self.dry_run)
        self.check_stale_locks(report)
        self.check_deadlocks(report)
        self.check_orphans(report)
        self.check_stuck_stages(report)
        self.check_no_progress(report, queued_depth=queued_depth)
        if pending_kills:
            self.escalate_pending(pending_kills)
        if report.deferred:
            emit_serve_event(
                self.operator,
                "serve.watchdog.budget_exhausted",
                level="warning",
                data={
                    "deferred": len(report.deferred),
                    "budget": self.config.watchdog.max_remediations_per_tick,
                    "rules": sorted({d.rule for d in report.deferred}),
                },
            )
        return report


__all__ = [
    "ALL_RULES",
    "RULE_DEADLOCK",
    "RULE_NO_PROGRESS",
    "RULE_ORPHAN",
    "RULE_STALE_LOCK",
    "RULE_STUCK_STAGE",
    "Remediation",
    "Watchdog",
    "WatchdogReport",
    "growth_idle_seconds",
]
