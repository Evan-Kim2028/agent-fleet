"""Core value types for ``agent-fleet merge-plan``.

Everything here is a frozen dataclass with a ``to_dict`` so a plan can be
serialised to JSON for ``--json`` output or a ``merge.plan`` event without
the caller knowing the internal shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Risk flags that force a PR into its own batch. Any one of these is enough.
RISK_FLAGS: tuple[str, ...] = ("migration", "deploy", "prod_write")

#: Marker for a gate approval line, e.g. ``PREMERGE-APPROVED 0dc2391ab``.
APPROVAL_PREFIX = "PREMERGE-APPROVED"

DEFAULT_MAX_BATCH_SIZE = 5


@dataclass(frozen=True)
class ApprovedPR:
    """A PR the gate has approved, before its change profile is built.

    ``approved_sha`` is the short SHA the gate approved.  ``head_sha`` is the
    SHA the PR head points at *now*; when the two disagree the approval is
    stale and the PR must not be batched.
    """

    repo: str
    pr_number: int
    approved_sha: str
    head_sha: str = ""
    operator: str = ""
    lane: str = ""
    source: str = ""

    @property
    def is_stale(self) -> bool:
        """True when the gate approved a SHA the PR head no longer points at."""
        return bool(self.head_sha) and not self.head_sha.startswith(self.approved_sha)

    @property
    def sha9(self) -> str:
        """The short SHA the operator's merge scripts are handed."""
        return self.approved_sha[:9]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "pr": self.pr_number,
            "approved_sha": self.approved_sha,
            "head_sha": self.head_sha,
            "operator": self.operator,
            "lane": self.lane,
            "source": self.source,
            "stale": self.is_stale,
        }


@dataclass(frozen=True)
class ChangeProfile:
    """What a PR changes, reduced to the facts that drive batching."""

    repo: str
    pr_number: int
    base_ref: str = ""
    files: tuple[str, ...] = ()
    deploy_unit: str = ""
    dbt_models: tuple[str, ...] = ()
    #: dbt models to pass to ``--select`` when this batch is rebuilt once.
    dbt_select: tuple[str, ...] = ()
    #: "manifest" when expanded via transform/target/manifest.json, else "direct".
    dbt_select_source: str = "direct"
    risk_flags: tuple[str, ...] = ()
    lines_changed: int = 0
    stale: bool = False
    reason: str = ""

    @property
    def is_risky(self) -> bool:
        return bool(self.risk_flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "pr": self.pr_number,
            "base_ref": self.base_ref,
            "files": list(self.files),
            "deploy_unit": self.deploy_unit,
            "dbt_models": list(self.dbt_models),
            "dbt_select": list(self.dbt_select),
            "dbt_select_source": self.dbt_select_source,
            "risk_flags": list(self.risk_flags),
            "lines_changed": self.lines_changed,
            "stale": self.stale,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Batch:
    """One group of PRs that ship together behind a single deploy + verify."""

    index: int
    repo: str
    prs: tuple[ApprovedPR, ...]
    deploy_unit: str
    reasons: tuple[str, ...] = ()
    dbt_select: tuple[str, ...] = ()
    executor_commands: tuple[str, ...] = ()
    #: True when a dbt model group had to be split by the size cap, so the
    #: downstream rebuild runs once per sub-batch instead of once overall.
    dbt_group_split: bool = False
    isolated_risk: bool = False

    @property
    def size(self) -> int:
        return len(self.prs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "repo": self.repo,
            "prs": [p.to_dict() for p in self.prs],
            "pr_numbers": [p.pr_number for p in self.prs],
            "size": self.size,
            "deploy_unit": self.deploy_unit,
            "reasons": list(self.reasons),
            "dbt_select": list(self.dbt_select),
            "dbt_select_source": "manifest" if self.dbt_select else "",
            "executor_commands": list(self.executor_commands),
            "dbt_group_split": self.dbt_group_split,
            "isolated_risk": self.isolated_risk,
        }


@dataclass(frozen=True)
class MergePlan:
    """The whole plan: ordered batches plus the PRs left out and why."""

    batches: tuple[Batch, ...] = ()
    excluded: tuple[ChangeProfile, ...] = ()
    notes: tuple[str, ...] = ()
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": "merge.plan",
            "max_batch_size": self.max_batch_size,
            "batch_count": len(self.batches),
            "merged_pr_count": sum(b.size for b in self.batches),
            "batches": [b.to_dict() for b in self.batches],
            "excluded": [p.to_dict() for p in self.excluded],
            "notes": list(self.notes),
        }


@dataclass
class RepoSpec:
    """Per-repo batching configuration, loaded from fleet.yaml.

    Kept mutable only because it is built once at load time; everything the
    planner consumes is copied into frozen types.
    """

    name: str
    path: str = ""
    #: prefix -> deploy unit, e.g. ``{"api/": "lor-api"}``. Longest prefix wins.
    deploy_units: dict[str, str] = field(default_factory=dict)
    #: Executor command templates, e.g. ``"scripts/merge.sh {pr_args}"``.
    merge_template: str = ""
    #: One command per PR, e.g. ``"scripts/merge_verify.sh {pr} {sha9}"``.
    merge_per_pr_template: str = ""
    dbt_manifest_path: str = "transform/target/manifest.json"
    risk_globs: tuple[str, ...] = ()
