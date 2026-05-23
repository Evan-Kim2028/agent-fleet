"""Map PR analysis JSON to fleet review contracts."""

from __future__ import annotations

from typing import Any

from agent_fleet.contracts.review import ReviewResult, ReviewVerdict


def risk_to_verdict(risk_level: str, findings: list[dict[str, Any]]) -> ReviewVerdict:
    level = risk_level.lower()
    if level == "critical":
        return ReviewVerdict.BLOCK
    severities = {str(item.get("severity", "low")).lower() for item in findings}
    if "critical" in severities:
        return ReviewVerdict.BLOCK
    if level == "high" or "high" in severities:
        return ReviewVerdict.REQUEST_CHANGES
    if level == "medium" or "medium" in severities:
        return ReviewVerdict.REQUEST_CHANGES
    return ReviewVerdict.APPROVE


def analysis_to_review_result(
    analysis: dict[str, Any],
    *,
    pr_number: int = 1,
) -> ReviewResult:
    findings = list(analysis.get("findings") or [])
    verdict = risk_to_verdict(str(analysis.get("risk_level", "low")), findings)
    issues = [
        {
            "severity": str(item.get("severity", "medium")),
            "file": str(item.get("file") or item.get("area") or ""),
            "message": str(item.get("message") or ""),
        }
        for item in findings
    ]
    return ReviewResult(
        pr_number=pr_number,
        verdict=verdict,
        summary=str(analysis.get("summary") or analysis.get("deep_analysis") or ""),
        issues=issues,
        shard_id=None,
    )
