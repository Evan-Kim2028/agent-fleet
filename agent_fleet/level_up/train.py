"""Mine experience and promote gated overlay rules."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from agent_fleet.level_up.experience import read_experience_rows
from agent_fleet.level_up.gate import GateResult, gate_after_tech_lead, gate_rule, infer_kind
from agent_fleet.level_up.journal import append_journal
from agent_fleet.level_up.models import LevelUpRule
from agent_fleet.level_up.overlay import (
    load_candidate,
    load_overlay,
    promote_rule,
    write_candidate,
)
from agent_fleet.level_up.paths import FLEET_TIER, persona_dir
from agent_fleet.tech_lead import skill_promotion_review

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class TrainCandidate:
    rule: LevelUpRule
    evidence_count: int
    weighted_evidence: float
    gate: GateResult
    candidate_id: str


@dataclass
class TrainResult:
    promoted: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:48] or "rule"


def _rule_id(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(text)}-{digest}"


def _area_from_files(changed_files: list[str]) -> str:
    if not changed_files:
        return ""
    first = changed_files[0].replace("\\", "/")
    if "/" in first:
        return first.rsplit("/", 1)[0] + "/"
    return ""


def _provenance_from_row(repo_key: str, row: dict[str, Any]) -> dict[str, Any]:
    goal = str(row.get("goal") or "dispatch task")
    if len(goal) > 120:
        goal = goal[:117] + "..."
    status = str(row.get("status") or row.get("review_verdict") or "experience")
    return {
        "repo_key": repo_key,
        "area": _area_from_files(list(row.get("changed_files") or ())),
        "task_summary": goal,
        "note": f"Learned from {repo_key} doing: {status}",
        "ts": row.get("ts"),
    }


def _rule_text_for_row(row: dict[str, Any]) -> tuple[str, str] | None:
    status = str(row.get("status") or "")
    review = str(row.get("review_verdict") or "")

    if status == "verify_failed" or "verify_failed" in status:
        return (
            "methodology",
            "Run repo verify_commands before claiming completion.",
        )
    if review in {"changes_requested", "request_changes"}:
        return (
            "review_quality",
            "Address review feedback with a minimal diff before re-requesting review.",
        )
    if status == "scope_violation":
        return (
            "methodology",
            "Keep changes scoped to the task goal; avoid unrelated edits.",
        )
    if status == "completed" and row.get("source") == "pr_loop":
        return (
            "methodology",
            "When fixing PR feedback, run verify after each fix round.",
        )
    return None


def _aggregate_candidates(
    repo_key: str,
    rows: list[dict[str, Any]],
) -> dict[str, tuple[LevelUpRule, int, float]]:
    grouped: dict[str, tuple[LevelUpRule, int, float]] = {}

    for row in rows:
        proposed = _rule_text_for_row(row)
        if proposed is None:
            continue

        kind, text = proposed
        rule_id = _rule_id(text)
        weight = float(row.get("weight") or 1.0)
        provenance = _provenance_from_row(repo_key, row)

        if rule_id in grouped:
            rule, count, total = grouped[rule_id]
            provenance_list = [*list(rule.provenance), provenance]
            grouped[rule_id] = (
                LevelUpRule(
                    id=rule.id,
                    kind=rule.kind,
                    text=rule.text,
                    provenance=tuple(provenance_list),
                    confidence=min(1.0, 0.5 + 0.1 * (count + 1)),
                ),
                count + 1,
                total + weight,
            )
            continue

        grouped[rule_id] = (
            LevelUpRule(
                id=rule_id,
                kind=kind,
                text=text,
                provenance=(provenance,),
                confidence=0.6,
            ),
            1,
            weight,
        )

    return grouped


def propose_train_candidates(
    repo_key: str,
    persona: str,
    *,
    limit: int = 200,
) -> list[TrainCandidate]:
    rows = read_experience_rows(repo_key, persona, limit=limit)
    overlay = load_overlay(repo_key, persona)
    existing_ids = {rule.id for rule in overlay.rules}
    existing_text = {rule.text.strip().lower() for rule in overlay.rules}

    candidates: list[TrainCandidate] = []
    for rule, evidence_count, weighted_evidence in _aggregate_candidates(repo_key, rows).values():
        if rule.id in existing_ids or rule.text.strip().lower() in existing_text:
            continue

        gate = gate_rule(
            rule,
            evidence_count=evidence_count,
            weighted_evidence=weighted_evidence,
        )
        candidate_id = rule.id
        candidates.append(
            TrainCandidate(
                rule=LevelUpRule(
                    id=rule.id,
                    kind=gate.kind or rule.kind or infer_kind(rule.text),
                    text=rule.text,
                    provenance=rule.provenance,
                    confidence=rule.confidence,
                ),
                evidence_count=evidence_count,
                weighted_evidence=weighted_evidence,
                gate=gate,
                candidate_id=candidate_id,
            )
        )
    return candidates


def train_persona(
    repo_key: str,
    persona: str,
    *,
    contribute_to_fleet: bool = True,
    dry_run: bool = False,
) -> TrainResult:
    """Mine experience, gate candidates, auto-promote or queue for tech lead."""
    result = TrainResult()
    candidates = propose_train_candidates(repo_key, persona)

    for item in candidates:
        gate_payload = {
            "passed": item.gate.passed,
            "kind": item.gate.kind,
            "reject_reason": item.gate.reject_reason,
            "needs_tech_lead": item.gate.needs_tech_lead,
            "evidence_count": item.evidence_count,
            "weighted_evidence": item.weighted_evidence,
        }
        append_journal(
            "level_up.gate.classify",
            repo_key,
            persona,
            data={"rule_id": item.rule.id, **gate_payload},
        )

        if not item.gate.passed and item.gate.needs_tech_lead:
            if dry_run:
                result.queued.append(item.candidate_id)
                continue
            write_candidate(
                repo_key,
                persona,
                item.candidate_id,
                item.rule,
                gate=gate_payload,
            )
            append_journal(
                "level_up.train.queued",
                repo_key,
                persona,
                data={"candidate_id": item.candidate_id, "kind": item.gate.kind},
            )
            result.queued.append(item.candidate_id)
            continue

        if not item.gate.passed:
            append_journal(
                "level_up.gate.reject",
                repo_key,
                persona,
                data={"rule_id": item.rule.id, "reason": item.gate.reject_reason},
            )
            result.rejected.append(item.rule.id)
            continue

        if dry_run:
            result.promoted.append(item.rule.id)
            continue

        promote_rule(repo_key, persona, item.rule)
        append_journal(
            "level_up.train.promoted",
            repo_key,
            persona,
            data={"rule_id": item.rule.id, "kind": item.gate.kind, "tier": "repo"},
        )
        result.promoted.append(item.rule.id)

        if contribute_to_fleet and item.gate.kind in {"methodology", "stack"}:
            fleet_overlay = load_overlay(FLEET_TIER, persona)
            if item.rule.id not in {r.id for r in fleet_overlay.rules}:
                promote_rule(FLEET_TIER, persona, item.rule)
                append_journal(
                    "level_up.train.promoted",
                    FLEET_TIER,
                    persona,
                    data={"rule_id": item.rule.id, "kind": item.gate.kind, "tier": "fleet"},
                )

    return result


def approve_candidate(
    repo_key: str,
    persona: str,
    candidate_id: str,
    *,
    contribute_to_fleet: bool = True,
) -> str:
    """Run tech-lead skill review on a queued candidate and promote if approved."""
    rule, gate_meta = load_candidate(repo_key, persona, candidate_id)
    kind = str(gate_meta.get("kind") or rule.kind or infer_kind(rule.text))

    review = skill_promotion_review(rule, kind=kind)
    append_journal(
        "level_up.gate.tech_lead",
        repo_key,
        persona,
        data={
            "candidate_id": candidate_id,
            "verdict": review.verdict,
            "summary": review.summary,
        },
    )

    final_gate = gate_after_tech_lead(kind=kind, verdict=review.verdict)
    candidate_path = persona_dir(repo_key, persona) / "candidates" / f"{candidate_id}.json"

    if not final_gate.passed:
        append_journal(
            "level_up.gate.reject",
            repo_key,
            persona,
            data={"rule_id": rule.id, "reason": final_gate.reject_reason},
        )
        if candidate_path.is_file():
            candidate_path.unlink()
        return review.verdict

    promote_rule(repo_key, persona, rule)
    append_journal(
        "level_up.train.promoted",
        repo_key,
        persona,
        data={"rule_id": rule.id, "kind": kind, "tier": "repo", "via": "approve"},
    )

    if contribute_to_fleet:
        promote_rule(FLEET_TIER, persona, rule)
        append_journal(
            "level_up.train.promoted",
            FLEET_TIER,
            persona,
            data={"rule_id": rule.id, "kind": kind, "tier": "fleet", "via": "approve"},
        )

    if candidate_path.is_file():
        candidate_path.unlink()
    return review.verdict


def find_overlay_overlap(repo_key: str, persona: str) -> list[dict[str, str]]:
    """Return rule ids present in both repo and fleet overlays."""
    fleet = load_overlay(FLEET_TIER, persona)
    repo = load_overlay(repo_key, persona)
    fleet_by_id = {rule.id: rule for rule in fleet.rules}
    overlaps: list[dict[str, str]] = []
    for rule in repo.rules:
        fleet_rule = fleet_by_id.get(rule.id)
        if fleet_rule is None:
            continue
        overlaps.append(
            {
                "id": rule.id,
                "repo_kind": rule.kind,
                "fleet_kind": fleet_rule.kind,
            }
        )
    return overlaps
