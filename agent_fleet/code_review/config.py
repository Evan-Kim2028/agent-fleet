"""Code review pipeline configuration from .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_fleet.pr_loop.config import PrLoopConfig


@dataclass(frozen=True)
class CodeReviewConfig:
    auto_fix: bool = False
    max_fix_attempts: int = 2
    fix_persona: str | None = None
    auto_push: bool = False
    auto_pr_loop: bool = False


def resolve_code_review_config(
    raw: dict[str, Any] | None,
    *,
    pr_loop: PrLoopConfig | None,
) -> CodeReviewConfig | None:
    """Load code_review section; inherit defaults from pr_loop when enabled."""
    section = (raw or {}).get("code_review")
    if section is False:
        return None

    inherited = CodeReviewConfig()
    if pr_loop and pr_loop.enabled and section is None:
        return CodeReviewConfig(
            auto_fix=True,
            max_fix_attempts=pr_loop.max_fix_attempts,
            fix_persona=pr_loop.fix_persona,
            auto_push=True,
            auto_pr_loop=True,
        )

    if not isinstance(section, dict):
        return None

    fix_persona = section.get("fix_persona") or (pr_loop.fix_persona if pr_loop else None)
    max_fix = int(section.get("max_fix_attempts") or (pr_loop.max_fix_attempts if pr_loop else 2))
    return CodeReviewConfig(
        auto_fix=bool(section.get("auto_fix", inherited.auto_fix)),
        max_fix_attempts=max_fix,
        fix_persona=str(fix_persona) if fix_persona else None,
        auto_push=bool(section.get("auto_push", inherited.auto_push)),
        auto_pr_loop=bool(section.get("auto_pr_loop", inherited.auto_pr_loop)),
    )
