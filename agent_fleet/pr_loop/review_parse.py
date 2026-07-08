"""Parse PR analyzer comments for blocking findings."""

from __future__ import annotations

import re

_BLOCKING_FINDING_PATTERN = re.compile(
    r"<b>\s*(MEDIUM|HIGH|CRITICAL)\s*</b>\s*\((\d+)\)",
    re.IGNORECASE,
)
_RISK_LEVEL_PATTERN = re.compile(
    r"\*\*Risk Level:\*\*[^\n]*\b(LOW|MEDIUM|HIGH|CRITICAL)\b",
    re.IGNORECASE,
)
_AGENT_FOOTER = "\U0001f916 Agent:"
_BLOCKING_RISK = {"MEDIUM", "HIGH", "CRITICAL"}
_SEVERITY_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

# Titles used by agent-fleet-pr-analyzer across backends (see github_action.backend_display).
# find_reviewer_comment accepts any of these plus an optional caller marker and
# always falls back to "**Risk Level:**" (present in format_comment output).
PR_ANALYSIS_MARKERS: tuple[str, ...] = (
    "Composer PR Analysis",
    "Grok PR Analysis",
    "Kimi PR Analysis",
    "OpenRouter PR Analysis",
    "Agent Fleet PR Analysis",
)


def is_agent_comment(body: str) -> bool:
    return _AGENT_FOOTER in body


def parse_review_risk(pr_comments: list[dict[str, object]]) -> str | None:
    """Return highest risk level from non-agent reviewer comments."""
    highest: str | None = None
    for comment in pr_comments:
        body = str(comment.get("body") or "")
        if is_agent_comment(body):
            continue
        match = _RISK_LEVEL_PATTERN.search(body)
        if not match:
            continue
        level = match.group(1).upper()
        if highest is None or _SEVERITY_ORDER[level] > _SEVERITY_ORDER[highest]:
            highest = level
    return highest


def has_blocking_findings(review_body: str, *, deletion_only: bool = False) -> bool:
    """True when review has medium+ findings that require a fix pass."""
    if deletion_only:
        return False
    for match in _BLOCKING_FINDING_PATTERN.finditer(review_body):
        if int(match.group(2)) > 0:
            return True
    overall = _RISK_LEVEL_PATTERN.search(review_body)
    return bool(overall and overall.group(1).upper() in _BLOCKING_RISK)


def _matches_review_marker(body: str, marker: str | None) -> bool:
    if marker and marker in body:
        return True
    if any(m in body for m in PR_ANALYSIS_MARKERS):
        return True
    return "**Risk Level:**" in body


def find_reviewer_comment(
    pr_comments: list[dict[str, object]],
    *,
    marker: str | None = None,
) -> str | None:
    """Return the latest non-agent PR analyzer comment.

    Matches the caller's *marker* (e.g. repo ``comment_title``), any known
    backend title (Composer/Grok/Kimi/OpenRouter/Agent Fleet), or the stable
    ``**Risk Level:**`` line always present in formatted analyses.
    """
    found: str | None = None
    for comment in pr_comments:
        body = str(comment.get("body") or "")
        if is_agent_comment(body):
            continue
        if _matches_review_marker(body, marker):
            found = body
    return found
