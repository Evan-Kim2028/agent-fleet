"""Unified fleet capacity configuration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_DEPRECATED_ISSUE_DISPATCH_CAPACITY_KEYS = (
    "max_in_flight_per_issue",
    "max_in_flight_visual_audit",
    "max_concurrent_dispatches",
    "max_concurrent_visual_audit",
    "min_available_ram_gb",
    "visual_audit_ram_gb",
)


@dataclass(frozen=True)
class CapacityTier:
    """Concurrency and RAM accounting for one dispatch class."""

    max_concurrent: int
    ram_gb: float = 0.0
    min_free_ram_gb: float = 0.0


@dataclass(frozen=True)
class PerIssueLimits:
    default: int = 3
    visual_audit: int = 1


@dataclass(frozen=True)
class RunCapacity:
    """Per-dispatch subprocess limits (RESEARCH threads, verify retries, etc.)."""

    max_research_workers: int = 4
    max_verify_retries: int = 3
    memory_limit_parent: str = "4G"
    memory_limit_research: str = "2G"


@dataclass(frozen=True)
class FleetCapacity:
    """Single source of truth for watcher admission and per-run limits."""

    max_dispatches: int = 4
    visual_audit: CapacityTier = CapacityTier(
        max_concurrent=2,
        ram_gb=6.0,
        min_free_ram_gb=8.0,
    )
    per_issue: PerIssueLimits = PerIssueLimits()
    run: RunCapacity = RunCapacity()

    @classmethod
    def defaults(cls) -> FleetCapacity:
        return cls()

    def slots_summary(self) -> dict[str, int | float]:
        """Human-readable capacity snapshot for logs and diagnostics."""
        return {
            "max_dispatches": self.max_dispatches,
            "visual_audit_max": self.visual_audit.max_concurrent,
            "visual_audit_ram_gb": self.visual_audit.ram_gb,
            "min_free_ram_gb": self.visual_audit.min_free_ram_gb,
            "per_issue_default": self.per_issue.default,
            "per_issue_visual_audit": self.per_issue.visual_audit,
            "max_research_workers": self.run.max_research_workers,
        }


def _tier(section: dict[str, Any], *, defaults: CapacityTier) -> CapacityTier:
    return CapacityTier(
        max_concurrent=int(section.get("max_concurrent", defaults.max_concurrent)),
        ram_gb=float(section.get("ram_gb", defaults.ram_gb)),
        min_free_ram_gb=float(section.get("min_free_ram_gb", defaults.min_free_ram_gb)),
    )


def load_capacity_config(raw: dict[str, Any] | None) -> FleetCapacity:
    """Load ``capacity`` from repo yaml; return defaults when absent."""
    defaults = FleetCapacity.defaults()
    section = (raw or {}).get("capacity")
    if not section:
        return defaults
    if not isinstance(section, dict):
        logger.warning("capacity must be a mapping; using defaults")
        return defaults

    tiers_raw = section.get("tiers")
    tiers: dict[str, Any] = tiers_raw if isinstance(tiers_raw, dict) else {}
    visual_audit_raw = tiers.get("visual_audit")
    visual_raw: dict[str, Any] = visual_audit_raw if isinstance(visual_audit_raw, dict) else {}
    per_issue_raw_obj = section.get("per_issue")
    per_issue_raw: dict[str, Any] = per_issue_raw_obj if isinstance(per_issue_raw_obj, dict) else {}
    run_raw_obj = section.get("run")
    run_raw: dict[str, Any] = run_raw_obj if isinstance(run_raw_obj, dict) else {}

    visual_audit = _tier(visual_raw, defaults=defaults.visual_audit)
    per_issue = PerIssueLimits(
        default=int(per_issue_raw.get("default", defaults.per_issue.default)),
        visual_audit=int(per_issue_raw.get("visual_audit", defaults.per_issue.visual_audit)),
    )
    run = RunCapacity(
        max_research_workers=int(
            run_raw.get("max_research_workers", defaults.run.max_research_workers),
        ),
        max_verify_retries=int(
            run_raw.get("max_verify_retries", defaults.run.max_verify_retries),
        ),
        memory_limit_parent=str(
            run_raw.get("memory_limit_parent", defaults.run.memory_limit_parent),
        ),
        memory_limit_research=str(
            run_raw.get("memory_limit_research", defaults.run.memory_limit_research),
        ),
    )

    return FleetCapacity(
        max_dispatches=int(section.get("max_dispatches", defaults.max_dispatches)),
        visual_audit=visual_audit,
        per_issue=per_issue,
        run=run,
    )


def warn_deprecated_issue_dispatch_capacity(section: dict[str, Any]) -> None:
    """Emit warnings when legacy capacity keys live under issue_dispatch."""
    for key in _DEPRECATED_ISSUE_DISPATCH_CAPACITY_KEYS:
        if key in section:
            logger.warning(
                "issue_dispatch.%s is removed; configure capacity.%s instead",
                key,
                _legacy_key_to_capacity(key),
            )


def _legacy_key_to_capacity(key: str) -> str:
    mapping = {
        "max_in_flight_per_issue": "per_issue.default",
        "max_in_flight_visual_audit": "per_issue.visual_audit",
        "max_concurrent_dispatches": "max_dispatches",
        "max_concurrent_visual_audit": "tiers.visual_audit.max_concurrent",
        "min_available_ram_gb": "tiers.visual_audit.min_free_ram_gb",
        "visual_audit_ram_gb": "tiers.visual_audit.ram_gb",
    }
    return mapping.get(key, key)
