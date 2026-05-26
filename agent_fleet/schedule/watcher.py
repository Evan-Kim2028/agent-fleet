"""Evaluate cron schedules and spawn fleet dispatches."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_fleet.capacity import (
    RETRYABLE_ADMISSION_REASONS,
    FleetCapacity,
    FleetCapacityGate,
    is_visual_audit_dispatch,
)
from agent_fleet.in_flight import reap_in_flight
from agent_fleet.issue_loop import github_ops
from agent_fleet.memory import available_ram_gb
from agent_fleet.schedule.cron import format_iso, is_due, next_fire_at, parse_iso
from agent_fleet.schedule.dispatch import (
    build_issue_comment,
    resolve_dispatch_workspace,
    spawn_issue_dispatch,
    spawn_task_dispatch,
    synthetic_issue_number,
)
from agent_fleet.state import load_state, now_iso, save_state, state_path

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.issue_loop.config import IssueDispatchConfig
    from agent_fleet.repo import RepoConfig
    from agent_fleet.schedule.config import ScheduleConfig, ScheduleJob

logger = logging.getLogger(__name__)


def schedule_jobs_state(state: dict[str, Any]) -> dict[str, Any]:
    return state.setdefault("schedules", {})


def reap_schedule_in_flight(state: dict[str, Any]) -> None:
    """Clear finished schedule job PIDs from schedules.*.in_flight."""
    from agent_fleet.in_flight import pid_is_fleet_dispatch

    schedules = state.get("schedules")
    if not isinstance(schedules, dict):
        return
    for entry in schedules.values():
        if not isinstance(entry, dict):
            continue
        inflight = entry.get("in_flight")
        if not isinstance(inflight, dict):
            continue
        pid = inflight.get("pid")
        if pid is None:
            entry.pop("in_flight", None)
            continue
        if not pid_is_fleet_dispatch(int(pid)):
            entry.pop("in_flight", None)


def job_state(state: dict[str, Any], job_id: str) -> dict[str, Any]:
    jobs = schedule_jobs_state(state)
    existing = jobs.get(job_id)
    if not isinstance(existing, dict):
        existing = {}
        jobs[job_id] = existing
    return existing


def _job_in_flight(entry: dict[str, Any]) -> bool:
    in_flight = entry.get("in_flight")
    if not isinstance(in_flight, dict):
        return False
    pid = in_flight.get("pid")
    if pid is None:
        return False
    from agent_fleet.in_flight import pid_is_fleet_dispatch

    return pid_is_fleet_dispatch(int(pid))


def _issue_in_flight(state: dict[str, Any], issue_number: int) -> bool:
    runs = (state.get("in_flight") or {}).get(str(issue_number)) or []
    return bool(runs)


def _ensure_next_due(job: ScheduleJob, entry: dict[str, Any]) -> None:
    if entry.get("next_due_at"):
        return
    entry["next_due_at"] = format_iso(next_fire_at(cron=job.cron, timezone=job.timezone))


def _advance_next_due(
    job: ScheduleJob,
    entry: dict[str, Any],
    *,
    from_time: datetime | None = None,
) -> None:
    base = from_time or datetime.now(UTC)
    entry["next_due_at"] = format_iso(
        next_fire_at(cron=job.cron, timezone=job.timezone, after=base)
    )


def _min_interval_blocks(job: ScheduleJob, entry: dict[str, Any]) -> bool:
    if job.policy.min_interval_s <= 0:
        return False
    last_run_at = entry.get("last_run_at")
    if not last_run_at:
        return False
    elapsed = time.time() - parse_iso(str(last_run_at)).timestamp()
    return elapsed < job.policy.min_interval_s


def _capacity_issue_number(job: ScheduleJob) -> int:
    if job.dispatch.kind == "issue" and job.dispatch.issue is not None:
        return job.dispatch.issue
    return synthetic_issue_number(job.id)


class ScheduleWatcher:
    """Poll cron schedules and spawn dispatches when due.

    The controller repo (*repo*) owns schedule config and ``.agent-fleet-state.json``.
    Each job may set ``dispatch.workspace`` to dispatch against a different target repo.
    """

    def __init__(
        self,
        repo: RepoConfig,
        schedule_config: ScheduleConfig,
        *,
        fleet_config_path: str | None = None,
    ) -> None:
        self.repo = repo
        self.config = schedule_config
        self.fleet_config_path = fleet_config_path
        self.state_file = state_path(repo.repo_root)

    def _target_root(self, job: ScheduleJob) -> Path:
        return resolve_dispatch_workspace(job=job, controller_root=self.repo.repo_root)

    def _target_repo(self, job: ScheduleJob) -> RepoConfig | None:
        from agent_fleet.repo import find_repo_config

        return find_repo_config(self._target_root(job))

    def _capacity_gate(self, job: ScheduleJob) -> FleetCapacityGate:
        target = self._target_repo(job)
        capacity = (target.capacity if target else None) or self.repo.capacity or FleetCapacity.defaults()
        return FleetCapacityGate(capacity)

    def _issue_dispatch_config(self, job: ScheduleJob) -> IssueDispatchConfig | None:
        target = self._target_repo(job)
        if target is not None and target.issue_dispatch is not None:
            return target.issue_dispatch
        return self.repo.issue_dispatch

    def list_jobs(self) -> list[dict[str, Any]]:
        state = load_state(self.state_file)
        rows: list[dict[str, Any]] = []
        for job in self.config.jobs:
            entry = job_state(state, job.id)
            _ensure_next_due(job, entry)
            rows.append(
                {
                    "id": job.id,
                    "enabled": job.enabled,
                    "cron": job.cron,
                    "timezone": job.timezone,
                    "kind": job.dispatch.kind,
                    "workspace": str(self._target_root(job)),
                    "next_due_at": entry.get("next_due_at"),
                    "last_run_at": entry.get("last_run_at"),
                    "last_status": entry.get("last_status"),
                    "in_flight": _job_in_flight(entry),
                }
            )
        save_state(self.state_file, state)
        return rows

    def poll_once(
        self,
        state: dict[str, Any] | None = None,
        *,
        force_job_id: str | None = None,
    ) -> list[dict[str, str]]:
        if state is None:
            state = load_state(self.state_file)
        reap_in_flight(state)
        reap_schedule_in_flight(state)
        results: list[dict[str, str]] = []
        retryable_deferred = False
        now = datetime.now(UTC)

        for job in self.config.jobs:
            if not job.enabled and force_job_id != job.id:
                continue
            if force_job_id and job.id != force_job_id:
                continue

            entry = job_state(state, job.id)
            _ensure_next_due(job, entry)

            if force_job_id is None and not is_due(next_due_at=entry.get("next_due_at"), now=now):
                continue

            if job.policy.skip_if_in_flight and _job_in_flight(entry):
                results.append({"job": job.id, "status": "schedule_in_flight"})
                continue

            issue_number = _capacity_issue_number(job)
            if (
                job.policy.skip_if_in_flight
                and job.dispatch.kind == "issue"
                and job.dispatch.issue is not None
                and _issue_in_flight(state, job.dispatch.issue)
            ):
                results.append({"job": job.id, "status": "issue_in_flight"})
                continue

            if force_job_id is None and _min_interval_blocks(job, entry):
                results.append({"job": job.id, "status": "min_interval"})
                continue

            issue_labels: list[str] = []
            issue_title = ""
            issue_body = ""
            if job.dispatch.kind == "issue" and job.dispatch.issue is not None:
                target_root = self._target_root(job)
                try:
                    issue = github_ops.issue_view(job.dispatch.issue, cwd=target_root)
                    issue_title = str(issue.get("title") or "")
                    issue_body = str(issue.get("body") or "")
                    issue_labels = github_ops.issue_labels(
                        job.dispatch.issue,
                        cwd=target_root,
                    )
                except Exception as exc:
                    logger.warning(
                        "Schedule %s: could not load issue #%s: %s",
                        job.id,
                        job.dispatch.issue,
                        exc,
                    )

            is_visual_audit = is_visual_audit_dispatch(
                issue_labels=issue_labels,
                title=issue_title,
                body=issue_body,
            )
            admission = self._capacity_gate(job).try_admit(
                state,
                issue_number=issue_number,
                persona=job.dispatch.persona,
                is_visual_audit=is_visual_audit,
                available_ram_gb=available_ram_gb(),
            )
            if not admission.allowed:
                retryable = admission.reason in RETRYABLE_ADMISSION_REASONS
                if retryable:
                    retryable_deferred = True
                results.append({"job": job.id, "status": admission.reason})
                logger.info(
                    "Schedule deferred job=%s reason=%s retryable=%s",
                    job.id,
                    admission.reason,
                    retryable,
                )
                continue

            pid = self._spawn(job)
            if pid is None:
                results.append({"job": job.id, "status": "spawn_failed"})
                continue

            in_flight = state.setdefault("in_flight", {}).setdefault(str(issue_number), [])
            in_flight.append(
                {
                    "pid": pid,
                    "persona": job.dispatch.persona,
                    "visual_audit": is_visual_audit,
                    "from_schedule": True,
                    "schedule_id": job.id,
                }
            )
            entry["in_flight"] = {"pid": pid, "started_at": now_iso()}
            entry["last_run_at"] = now_iso()
            entry["last_status"] = "dispatched"
            _advance_next_due(job, entry, from_time=now)
            results.append({"job": job.id, "status": "dispatched", "pid": str(pid)})

            if force_job_id is not None:
                break

            if job.policy.missed == "catch_up_all" and is_due(
                next_due_at=entry.get("next_due_at"), now=now
            ):
                logger.info("Schedule %s catch_up_all: additional fire may occur next poll", job.id)

        save_state(self.state_file, state)
        if retryable_deferred:
            results.append({"status": "retryable_deferred"})
        return results

    def _spawn(self, job: ScheduleJob) -> int | None:
        dispatch = job.dispatch
        target_root = self._target_root(job)
        if dispatch.kind == "issue":
            issue_dispatch = self._issue_dispatch_config(job)
            if dispatch.issue is None or issue_dispatch is None:
                logger.error(
                    "Schedule %s issue kind requires issue_dispatch on target %s",
                    job.id,
                    target_root,
                )
                return None
            comment = build_issue_comment(job, issue_dispatch)
            return spawn_issue_dispatch(
                issue_number=dispatch.issue,
                comment_body=comment,
                persona=dispatch.persona,
                repo_root=target_root,
            )
        return spawn_task_dispatch(
            job_id=job.id,
            dispatch=dispatch,
            repo_root=target_root,
            fleet_config_path=self.fleet_config_path,
        )


def run_schedule_once(
    workspace: Path,
    *,
    fleet_config_path: str | None = None,
    force_job_id: str | None = None,
) -> list[dict[str, str]]:
    from agent_fleet.repo import find_repo_config

    repo = find_repo_config(workspace)
    if repo is None or repo.schedules is None or not repo.schedules.enabled:
        return [{"status": "disabled"}]
    watcher = ScheduleWatcher(
        repo,
        repo.schedules,
        fleet_config_path=fleet_config_path,
    )
    return watcher.poll_once(force_job_id=force_job_id)
