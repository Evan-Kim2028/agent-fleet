"""Parse PR analyzer comment bodies into :class:`ReviewEvidence`."""

from __future__ import annotations

import re

from agent_fleet.autonomy.types import Finding, ReviewEvidence, Severity

# Keep in lockstep with agent_fleet.pr_loop.review_parse (parity tests).
_BLOCKING_FINDING_PATTERN = re.compile(
    r"<b>\s*(MEDIUM|HIGH|CRITICAL)\s*</b>\s*\((\d+)\)",
    re.IGNORECASE,
)
_RISK_LEVEL_PATTERN = re.compile(
    r"\*\*Risk Level:\*\*[^\n]*\b(LOW|MEDIUM|HIGH|CRITICAL)\b",
    re.IGNORECASE,
)
_BLOCKING_RISK = frozenset({"MEDIUM", "HIGH", "CRITICAL"})


def parse_review_body(
    body: str,
    *,
    head_sha: str | None = None,
    producer_id: str | None = None,
) -> ReviewEvidence:
    """Parse a review comment body into structured :class:`ReviewEvidence`.

    Extracts overall **Risk Level:** and synthetic :class:`Finding` rows for
    each MEDIUM/HIGH/CRITICAL HTML count bucket (``<b>SEVERITY</b> (N)``).
    """
    findings: list[Finding] = []
    for match in _BLOCKING_FINDING_PATTERN.finditer(body):
        severity = match.group(1).upper()
        count = int(match.group(2))
        findings.append(
            Finding(
                severity=severity,  # type: ignore[arg-type]
                count=count,
            )
        )

    overall: str | None = None
    risk_match = _RISK_LEVEL_PATTERN.search(body)
    if risk_match:
        overall = risk_match.group(1).upper()

    raw_marker: str | None = None
    if "**Risk Level:**" in body:
        raw_marker = "**Risk Level:**"

    return ReviewEvidence(
        head_sha=head_sha,
        findings=tuple(findings),
        overall_risk=overall,
        raw_marker=raw_marker,
        producer_id=producer_id,
    )


def review_is_blocking(
    review: ReviewEvidence,
    *,
    deletion_only: bool = False,
) -> bool:
    """True when review has medium+ findings that require a fix pass.

    Parity with :func:`agent_fleet.pr_loop.review_parse.has_blocking_findings`
    for bodies parsed via :func:`parse_review_body`.
    """
    if deletion_only:
        return False
    for finding in review.findings:
        if finding.severity in _BLOCKING_RISK and (finding.count is None or finding.count > 0):
            return True
    return bool(review.overall_risk and review.overall_risk.upper() in _BLOCKING_RISK)


def body_is_blocking(body: str, *, deletion_only: bool = False) -> bool:
    """Convenience: parse *body* and return whether it is blocking."""
    return review_is_blocking(parse_review_body(body), deletion_only=deletion_only)


def has_security_medium_plus(review: ReviewEvidence) -> bool:
    """True when any finding is category=security at MEDIUM+ severity."""
    for finding in review.findings:
        if finding.category == "security" and finding.severity in _BLOCKING_RISK:
            return True
    return False


def severity_rank(level: str | None) -> int:
    order: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    if not level:
        return -1
    return order.get(level.upper(), -1)


def normalize_severity(level: str) -> Severity | None:
    upper = level.upper()
    if upper in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        return upper  # type: ignore[return-value]
    return None
