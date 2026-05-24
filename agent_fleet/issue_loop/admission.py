"""Admission checks for issue-loop dispatches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_fleet.issue_loop.config import IssueDispatchConfig


@dataclass(frozen=True)
class DispatchAdmission:
    allowed: bool
    reason: str


def iter_in_flight_runs(state: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for issue_key, issue_runs in (state.get("in_flight") or {}).items():
        if not isinstance(issue_runs, list):
            continue
        for run in issue_runs:
            if isinstance(run, dict):
                runs.append({"issue": issue_key, **run})
    return runs


def count_in_flight(
    state: dict[str, Any],
    *,
    visual_audit_only: bool = False,
) -> int:
    runs = iter_in_flight_runs(state)
    if not visual_audit_only:
        return len(runs)
    return sum(1 for run in runs if run.get("visual_audit"))


def check_dispatch_admission(
    config: IssueDispatchConfig,
    state: dict[str, Any],
    *,
    issue_number: int,
    persona: str,
    is_visual_audit: bool,
    available_ram_gb: float | None,
) -> DispatchAdmission:
    in_flight = state.setdefault("in_flight", {}).setdefault(str(issue_number), [])
    if any(run.get("persona") == persona for run in in_flight):
        return DispatchAdmission(False, "already_in_flight")

    per_issue_limit = (
        config.max_in_flight_visual_audit
        if is_visual_audit
        else config.max_in_flight_per_issue
    )
    if len(in_flight) >= per_issue_limit:
        return DispatchAdmission(False, "issue_at_capacity")

    total = count_in_flight(state)
    if total >= config.max_concurrent_dispatches:
        return DispatchAdmission(False, "fleet_at_capacity")

    if is_visual_audit:
        visual_audit_count = count_in_flight(state, visual_audit_only=True)
        if visual_audit_count >= config.max_concurrent_visual_audit:
            return DispatchAdmission(False, "visual_audit_at_capacity")

        if available_ram_gb is not None and available_ram_gb < config.min_available_ram_gb:
            return DispatchAdmission(False, "insufficient_ram")

        reserved = (visual_audit_count + 1) * config.visual_audit_ram_gb
        if available_ram_gb is not None and available_ram_gb < reserved:
            return DispatchAdmission(False, "visual_audit_ram_reserved")

    return DispatchAdmission(True, "ok")
