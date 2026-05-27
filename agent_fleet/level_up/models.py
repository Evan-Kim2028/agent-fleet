"""Dataclasses for persona level-up storage and dispatch equip."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LevelUpRule:
    id: str
    kind: str
    text: str
    pinned: bool = False
    stack_tags: tuple[str, ...] = ()
    area_patterns: tuple[str, ...] = ()
    provenance: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LevelUpRule:
        return cls(
            id=str(raw.get("id", "")),
            kind=str(raw.get("kind", "")),
            text=str(raw.get("text", "")),
            pinned=bool(raw.get("pinned", False)),
            stack_tags=tuple(raw.get("stack_tags") or ()),
            area_patterns=tuple(raw.get("area_patterns") or ()),
            provenance=tuple(raw.get("provenance") or ()),
            confidence=float(raw.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class LevelUpOverlay:
    schema_version: int
    rules: tuple[LevelUpRule, ...]
    generation: int = 0


@dataclass(frozen=True)
class DispatchEquip:
    skill_slots_execute: tuple[str, ...]
    skill_slots_review: tuple[str, ...]
    level_up_generation: int
    parent_run_id: str | None = None
    persona: str = ""
    base_loadout: str = ""
    compose_body: str = ""


@dataclass(frozen=True)
class ExperienceEntry:
    source: str
    weight: float
    pr_loop_round: int | None = None
    status: str | None = None
    goal: str | None = None
    review_verdict: str | None = None
    equip_snapshot: dict[str, Any] = field(default_factory=dict)
    changed_files: tuple[str, ...] = ()
    run_id: str | None = None
    repo_key: str | None = None
    persona: str | None = None
    outcome_metrics: dict[str, Any] = field(default_factory=dict)
