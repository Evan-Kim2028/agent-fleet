"""Types for the autonomy control plane (pure evidence → Decision)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

Severity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
Category = Literal[
    "security",
    "correctness",
    "availability",
    "docs_nit",
    "style",
    "ops",
    "unknown",
]


@dataclass(frozen=True)
class Finding:
    """One review finding or a synthetic count bucket from a review comment."""

    severity: Severity
    category: Category | None = None
    path: str | None = None
    count: int | None = None


@dataclass(frozen=True)
class ReviewEvidence:
    """Structured evidence extracted from a PR analyzer (or similar) comment."""

    head_sha: str | None = None
    findings: tuple[Finding, ...] = ()
    overall_risk: str | None = None
    raw_marker: str | None = None
    producer_id: str | None = None


@dataclass(frozen=True)
class CiEvidence:
    """CI status for non-ignored required checks on a PR head."""

    head_sha: str | None = None
    required_checks: dict[str, str] = field(default_factory=dict)
    all_non_ignored_green: bool = False
    pending: bool = False
    ready: bool = True


@dataclass(frozen=True)
class PathEvidence:
    """Changed files vs critical path prefixes (park gate)."""

    changed_files: tuple[str, ...] = ()
    critical_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutonomyEvidence:
    """Full evidence bundle for :func:`agent_fleet.autonomy.decide.decide`."""

    review: ReviewEvidence | None = None
    ci: CiEvidence | None = None
    paths: PathEvidence | None = None
    review_addressed_for_sha: str | None = None
    pr_head_sha: str | None = None
    deletion_only: bool = False


class Action(StrEnum):
    """Next action the PR loop / watcher should take."""

    WAIT_REVIEW = "WAIT_REVIEW"
    FIX_REVIEW = "FIX_REVIEW"
    FIX_CI = "FIX_CI"
    PARK = "PARK"
    MERGE = "MERGE"
    NOOP = "NOOP"


@dataclass(frozen=True)
class Decision:
    """Result of autonomy evaluation."""

    action: Action
    reason: str
    park_reason: str | None = None
