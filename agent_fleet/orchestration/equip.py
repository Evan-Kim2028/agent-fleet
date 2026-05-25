"""Resolve dispatch equip: loadouts, overlays, dynamic skills, journaling."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.level_up.compaction import touch_overlay_rules
from agent_fleet.level_up.experience import last_experience_shows_verify_failed
from agent_fleet.level_up.journal import append_journal
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.level_up.overlay import compose_overlay_text, load_overlay
from agent_fleet.level_up.paths import FLEET_TIER, repo_key
from agent_fleet.skills_lib import (
    SYSTEMATIC_DEBUGGING_SKILL,
    compose_persona_body,
    load_loadout,
    loadout_execute_skill_ids,
    loadout_review_skill_ids,
    skill_exists_in_base_kit,
)

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import FleetTask
    from agent_fleet.repo import RepoConfig


def resolve_dispatch_equip(
    task: FleetTask,
    fleet_config: FleetConfig,
    repo: RepoConfig | None,
    run_id: str | None = None,
) -> DispatchEquip:
    """Pick execute/review skill slots and compose body for one dispatch."""
    del fleet_config  # reserved for fleet-wide equip policy
    persona = task.persona or "coder"
    loadout = load_loadout(persona)
    loadout_name = str(loadout.get("name") or persona)

    repo_key_value = repo_key(
        name=repo.name if repo else None,
        repo_root=repo.repo_root if repo else None,
    )
    fleet_overlay = load_overlay("_fleet", persona)
    repo_overlay = load_overlay(repo_key_value, persona)
    fleet_overlay_text = compose_overlay_text(fleet_overlay.rules)
    repo_overlay_text = compose_overlay_text(repo_overlay.rules)
    generation = max(fleet_overlay.generation, repo_overlay.generation)

    fleet_rule_ids = [rule.id for rule in fleet_overlay.rules]
    repo_rule_ids = [rule.id for rule in repo_overlay.rules]
    if fleet_rule_ids:
        touch_overlay_rules(FLEET_TIER, persona, fleet_rule_ids)
    if repo_rule_ids:
        touch_overlay_rules(repo_key_value, persona, repo_rule_ids)

    loadout_execute = loadout_execute_skill_ids(loadout)
    extra_execute: list[str] = []
    if (
        last_experience_shows_verify_failed(repo_key_value, persona)
        and skill_exists_in_base_kit(SYSTEMATIC_DEBUGGING_SKILL)
        and SYSTEMATIC_DEBUGGING_SKILL not in loadout_execute
    ):
        extra_execute.append(SYSTEMATIC_DEBUGGING_SKILL)

    skill_slots_execute = [*loadout_execute, *extra_execute]
    skill_slots_review = loadout_review_skill_ids(loadout)
    parent_run_id = task.equip.parent_run_id if task.equip else None

    compose_body = compose_persona_body(
        loadout,
        fleet_overlay=fleet_overlay_text,
        repo_overlay=repo_overlay_text,
        extra_skills=extra_execute or None,
        level_up_generation=generation,
    )

    equip = DispatchEquip(
        persona=persona,
        base_loadout=loadout_name,
        skill_slots_execute=tuple(skill_slots_execute),
        skill_slots_review=tuple(skill_slots_review),
        level_up_generation=generation,
        parent_run_id=parent_run_id,
        compose_body=compose_body,
    )

    append_journal(
        "equip.loadout",
        repo_key_value,
        persona,
        run_id=run_id,
        data={
            "base_loadout": loadout_name,
            "skill_slots_execute": list(equip.skill_slots_execute),
            "skill_slots_review": list(equip.skill_slots_review),
            "parent_run_id": parent_run_id,
        },
    )
    append_journal(
        "equip.compose",
        repo_key_value,
        persona,
        run_id=run_id,
        data={
            "level_up_generation": generation,
            "compose_chars": len(compose_body),
            "parent_run_id": parent_run_id,
        },
    )
    return equip
