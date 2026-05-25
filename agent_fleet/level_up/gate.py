# ruff: noqa: TC001
"""Skill vs memory gatekeeping for level-up promotion."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_fleet.level_up.models import LevelUpRule

_EPISODIC_PATTERNS = (
    re.compile(r"issue\s*#\d+", re.I),
    re.compile(r"\bPR\s*#\d+", re.I),
    re.compile(r"\bbranch\s+[a-z0-9/_-]+\b", re.I),
    re.compile(r"^\s*remember\s+", re.I),
)

_METHODology_KEYWORDS = (
    "verify",
    "test",
    "lint",
    "before claiming",
    "minimal diff",
    "scope",
    "run ",
)
_DOMAIN_KEYWORDS = (
    "iceberg",
    "parquet",
    "lake",
    "partition",
    "schema",
    "migration",
    "api",
)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    kind: str
    reject_reason: str | None = None
    needs_tech_lead: bool = False
    evidence_ok: bool = False


def _looks_episodic(text: str) -> str | None:
    stripped = text.strip()
    if len(stripped) < 12:
        return "too_short"
    for pattern in _EPISODIC_PATTERNS:
        if pattern.search(stripped):
            return "episodic_pattern"
    if re.fullmatch(r"[\w./-]+\.(py|ts|js|go|rs)", stripped):
        return "single_path_only"
    return None


def infer_kind(text: str) -> str:
    lower = text.lower()
    if any(k in lower for k in _DOMAIN_KEYWORDS):
        return "domain_data"
    if any(k in lower for k in _METHODology_KEYWORDS):
        return "methodology"
    return "stack"


def gate_rule(
    rule: LevelUpRule,
    *,
    evidence_count: int = 1,
    weighted_evidence: float = 1.0,
) -> GateResult:
    """Classify and gate a candidate skill rule."""
    episodic = _looks_episodic(rule.text)
    if episodic:
        return GateResult(
            passed=False,
            kind=rule.kind or infer_kind(rule.text),
            reject_reason=episodic,
        )

    kind = rule.kind or infer_kind(rule.text)
    evidence_ok = evidence_count >= 1 and weighted_evidence >= 1.0
    if not evidence_ok:
        return GateResult(
            passed=False,
            kind=kind,
            reject_reason="insufficient_evidence",
            evidence_ok=False,
        )

    needs_tech_lead = kind in {"domain_data", "domain_app", "review_quality"}
    auto_ok = kind in {"methodology", "stack"} and not needs_tech_lead

    if auto_ok:
        return GateResult(passed=True, kind=kind, evidence_ok=True, needs_tech_lead=False)

    return GateResult(
        passed=False,
        kind=kind,
        reject_reason="needs_tech_lead",
        evidence_ok=True,
        needs_tech_lead=True,
    )


def gate_after_tech_lead(
    *,
    kind: str,
    verdict: str,
) -> GateResult:
    if verdict == "approve":
        return GateResult(
            passed=True,
            kind=kind,
            evidence_ok=True,
            needs_tech_lead=True,
        )
    return GateResult(
        passed=False,
        kind=kind,
        reject_reason=f"tech_lead_{verdict}",
        needs_tech_lead=True,
    )
