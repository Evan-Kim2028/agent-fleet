"""Schedule configuration from .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

DispatchKind = Literal["issue", "task"]
MissedPolicy = Literal["skip", "catch_up_once", "catch_up_all"]


@dataclass(frozen=True)
class ScheduleDispatchConfig:
    kind: DispatchKind
    workspace: str | None = None
    issue: int | None = None
    persona: str = "coder"
    pipeline: str = "code_review"
    goal: str = ""
    context: str = ""
    note: str = ""


@dataclass(frozen=True)
class SchedulePolicyConfig:
    skip_if_in_flight: bool = True
    missed: MissedPolicy = "skip"
    min_interval_s: int = 0


@dataclass(frozen=True)
class ScheduleJob:
    id: str
    cron: str
    timezone: str = "UTC"
    enabled: bool = True
    dispatch: ScheduleDispatchConfig = field(
        default_factory=lambda: ScheduleDispatchConfig(kind="issue")
    )
    policy: SchedulePolicyConfig = field(default_factory=SchedulePolicyConfig)


@dataclass
class ScheduleConfig:
    enabled: bool = False
    poll_interval_s: int = 60
    jobs: list[ScheduleJob] = field(default_factory=list)


def _load_dispatch(raw: dict[str, Any]) -> ScheduleDispatchConfig:
    kind_raw = str(raw.get("kind", "issue"))
    kind: DispatchKind = "task" if kind_raw == "task" else "issue"
    issue_raw = raw.get("issue")
    issue = int(issue_raw) if issue_raw is not None else None
    return ScheduleDispatchConfig(
        kind=kind,
        workspace=str(raw.get("workspace")).strip() if raw.get("workspace") else None,
        issue=issue,
        persona=str(raw.get("persona") or "coder"),
        pipeline=str(raw.get("pipeline") or "code_review"),
        goal=str(raw.get("goal") or ""),
        context=str(raw.get("context") or ""),
        note=str(raw.get("note") or ""),
    )


def _load_policy(raw: dict[str, Any] | None) -> SchedulePolicyConfig:
    section = raw or {}
    missed_raw = str(section.get("missed", "skip"))
    if missed_raw == "catch_up_once":
        missed: MissedPolicy = "catch_up_once"
    elif missed_raw == "catch_up_all":
        missed = "catch_up_all"
    else:
        missed = "skip"
    return SchedulePolicyConfig(
        skip_if_in_flight=bool(section.get("skip_if_in_flight", True)),
        missed=missed,
        min_interval_s=int(section.get("min_interval_s", 0)),
    )


def _load_job(raw: dict[str, Any]) -> ScheduleJob | None:
    job_id = str(raw.get("id") or "").strip()
    cron = str(raw.get("cron") or "").strip()
    if not job_id or not cron:
        return None
    dispatch_raw = raw.get("dispatch")
    dispatch = _load_dispatch(dispatch_raw if isinstance(dispatch_raw, dict) else {})
    if dispatch.kind == "issue" and dispatch.issue is None:
        return None
    if dispatch.kind == "task" and not dispatch.goal.strip():
        return None
    policy_raw = raw.get("policy")
    policy = _load_policy(policy_raw if isinstance(policy_raw, dict) else None)
    return ScheduleJob(
        id=job_id,
        cron=cron,
        timezone=str(raw.get("timezone") or "UTC"),
        enabled=bool(raw.get("enabled", True)),
        dispatch=dispatch,
        policy=policy,
    )


def load_schedule_config(_repo_root: Path, raw: dict[str, Any] | None) -> ScheduleConfig | None:
    section = (raw or {}).get("schedules")
    if not section:
        return None
    if not isinstance(section, dict):
        return None
    jobs: list[ScheduleJob] = []
    for entry in section.get("jobs") or []:
        if isinstance(entry, dict):
            job = _load_job(entry)
            if job is not None:
                jobs.append(job)
    return ScheduleConfig(
        enabled=bool(section.get("enabled", False)),
        poll_interval_s=int(section.get("poll_interval_s", 60)),
        jobs=jobs,
    )
