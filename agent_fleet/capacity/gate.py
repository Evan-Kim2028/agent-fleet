"""Dispatch admission against FleetCapacity."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_fleet.capacity.config import FleetCapacity


@dataclass(frozen=True)
class AdmissionResult:
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


def count_visual_in_flight(state: dict[str, Any]) -> int:
    return count_in_flight(state, visual_audit_only=True)


RETRYABLE_ADMISSION_REASONS = frozenset(
    {
        "fleet_at_capacity",
        "visual_audit_at_capacity",
        "insufficient_ram",
        "visual_audit_ram_reserved",
    },
)


class FleetCapacityGate:
    """Enforce repo capacity limits before spawning a dispatch subprocess."""

    def __init__(self, capacity: FleetCapacity) -> None:
        self.capacity = capacity

    def try_admit(
        self,
        state: dict[str, Any],
        *,
        issue_number: int,
        persona: str,
        is_visual_audit: bool,
        available_ram_gb: float | None,
    ) -> AdmissionResult:
        in_flight = state.setdefault("in_flight", {}).setdefault(str(issue_number), [])
        if any(run.get("persona") == persona for run in in_flight):
            return AdmissionResult(False, "already_in_flight")

        per_issue_limit = (
            self.capacity.per_issue.visual_audit
            if is_visual_audit
            else self.capacity.per_issue.default
        )
        if len(in_flight) >= per_issue_limit:
            return AdmissionResult(False, "issue_at_capacity")

        total = count_in_flight(state)
        if total >= self.capacity.max_dispatches:
            return AdmissionResult(False, "fleet_at_capacity")

        if is_visual_audit:
            tier = self.capacity.visual_audit
            visual_audit_count = count_visual_in_flight(state)
            if visual_audit_count >= tier.max_concurrent:
                return AdmissionResult(False, "visual_audit_at_capacity")

            if available_ram_gb is not None and available_ram_gb < tier.min_free_ram_gb:
                return AdmissionResult(False, "insufficient_ram")

            reserved = (visual_audit_count + 1) * tier.ram_gb
            if available_ram_gb is not None and available_ram_gb < reserved:
                return AdmissionResult(False, "visual_audit_ram_reserved")

        return AdmissionResult(True, "ok")
