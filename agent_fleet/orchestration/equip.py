"""Resolve dispatch equip: loadouts, overlays, dynamic skills, journaling.

Call :func:`resolve_dispatch_equip` once per dispatch (dispatcher, PR loop, fix phase
when ``fix_persona != task.persona``). Reuse ``task.equip`` on the execute and
code-review fix fast paths when the persona matches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_fleet.level_up.compaction import touch_overlay_rules
from agent_fleet.level_up.experience import last_experience_shows_verify_failed
from agent_fleet.level_up.journal import append_journal
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.level_up.overlay import compose_overlay_text, load_overlay
from agent_fleet.level_up.paths import FLEET_TIER, repo_key
from agent_fleet.skills_lib import (
    PR_LOOP_EXECUTE_SKILLS,
    SYSTEMATIC_DEBUGGING_SKILL,
    base_kit_skill_dirs,
    compose_persona_body,
    load_loadout,
    loadout_execute_skill_ids,
    loadout_review_skill_ids,
    merge_skill_dirs,
    skill_exists_in_base_kit,
)

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import FleetTask
    from agent_fleet.repo import RepoConfig


def _empty_loadout(persona: str) -> dict[str, Any]:
    return {"name": persona, "skill_slots": {"execute": [], "review": []}}


def _resolve_persona_loadout(
    persona: str,
    *,
    personas_dir: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Return loadout dict and display name; markdown-only repos get an empty loadout."""
    kwargs: dict[str, Any] = {}
    if personas_dir is not None:
        kwargs["personas_dir"] = personas_dir
    try:
        loadout = load_loadout(persona, **kwargs)
    except FileNotFoundError:
        return _empty_loadout(persona), persona
    return loadout, str(loadout.get("name") or persona)


def resolve_dispatch_equip(
    task: FleetTask,
    fleet_config: FleetConfig,
    repo: RepoConfig | None,
    run_id: str | None = None,
) -> DispatchEquip:
    """Resolve dispatch equip: loadouts, overlays, dynamic skills, journaling."""
    persona = task.persona or "coder"
    personas_dir = fleet_config.personas_dir
    if repo is not None and repo.personas_dir is not None:
        personas_dir = repo.personas_dir
    loadout, loadout_name = _resolve_persona_loadout(persona, personas_dir=personas_dir)

    skill_dirs = merge_skill_dirs(base_kit_skill_dirs(), fleet_config.skill_dirs)

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

    if repo is not None and repo.pr_loop is not None and repo.pr_loop.enabled:
        for skill_id in PR_LOOP_EXECUTE_SKILLS:
            if (
                skill_exists_in_base_kit(skill_id)
                and skill_id not in loadout_execute
                and skill_id not in extra_execute
            ):
                extra_execute.append(skill_id)

    skill_slots_execute = [*loadout_execute, *extra_execute]
    skill_slots_review = loadout_review_skill_ids(loadout)
    parent_run_id = task.equip.parent_run_id if task.equip else None

    compose_body = compose_persona_body(
        loadout,
        fleet_overlay=fleet_overlay_text,
        repo_overlay=repo_overlay_text,
        extra_skills=extra_execute or None,
        level_up_generation=generation,
        skill_dirs=skill_dirs,
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
