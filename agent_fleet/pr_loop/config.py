"""PR loop configuration from .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class PrLoopConfig:
    """Settings for automated review-fix-merge loop."""

    enabled: bool = False
    branch_prefixes: tuple[str, ...] = ("fleet/",)
    poll_interval_s: int = 10
    review_poll_s: int = 10
    ci_poll_s: int = 10
    ci_register_poll_s: int = 5
    post_fix_poll_s: int = 15
    review_poll_timeout_s: int = 1800
    ci_poll_timeout_s: int = 1800
    max_fix_attempts: int = 2
    max_ci_fix_attempts: int = 2
    max_ci_timeout_attempts: int = 3
    merge_cooldown_s: int = 300
    tiered_merge_gate: bool = False
    auto_merge: bool = True
    fix_persona: str | None = None
    ci_fix_persona: str | None = None
    ignored_ci_checks: tuple[str, ...] = field(
        default_factory=lambda: ("composer pr analysis", "kimi pr analysis")
    )
    needs_human_review_label: str = "needs-human-review"
    state_file: str = ".agent-fleet-loop-state.json"
    worktree_reaper_max_age_s: int = 86400


def load_pr_loop_config(_repo_root: Path, raw: dict[str, Any] | None) -> PrLoopConfig | None:
    section = (raw or {}).get("pr_loop")
    if not section:
        return None
    if not isinstance(section, dict):
        return None

    branch_prefixes = section.get("branch_prefixes") or ["fleet/"]
    ignored = section.get("ignored_ci_checks") or [
        "composer pr analysis",
        "kimi pr analysis",
    ]
    return PrLoopConfig(
        enabled=bool(section.get("enabled", False)),
        branch_prefixes=tuple(str(p) for p in branch_prefixes),
        poll_interval_s=int(section.get("poll_interval_s", 10)),
        review_poll_s=int(section.get("review_poll_s", 10)),
        ci_poll_s=int(section.get("ci_poll_s", 10)),
        ci_register_poll_s=int(section.get("ci_register_poll_s", 5)),
        post_fix_poll_s=int(section.get("post_fix_poll_s", 15)),
        review_poll_timeout_s=int(section.get("review_poll_timeout_s", 1800)),
        ci_poll_timeout_s=int(section.get("ci_poll_timeout_s", 1800)),
        max_fix_attempts=int(section.get("max_fix_attempts", 2)),
        max_ci_fix_attempts=int(section.get("max_ci_fix_attempts", 2)),
        max_ci_timeout_attempts=int(section.get("max_ci_timeout_attempts", 3)),
        merge_cooldown_s=int(section.get("merge_cooldown_s", 300)),
        tiered_merge_gate=bool(section.get("tiered_merge_gate", False)),
        auto_merge=bool(section.get("auto_merge", True)),
        fix_persona=(str(section["fix_persona"]) if section.get("fix_persona") else None),
        ci_fix_persona=(str(section["ci_fix_persona"]) if section.get("ci_fix_persona") else None),
        ignored_ci_checks=tuple(str(c).lower() for c in ignored),
        needs_human_review_label=str(section.get("needs_human_review_label", "needs-human-review")),
        state_file=str(section.get("state_file", ".agent-fleet-loop-state.json")),
        worktree_reaper_max_age_s=int(section.get("worktree_reaper_max_age_s", 86400)),
    )
