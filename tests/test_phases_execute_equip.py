"""Execute phase uses dispatch equip compose body."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.hooks import FleetTask, Persona
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.phases import _build_execute_prompt


def test_execute_prompt_prefers_equip_compose_body() -> None:
    persona = Persona(
        name="coder",
        prompt_path=Path("coder.md"),
        allowed_tools=[],
        capabilities={},
        body="Static persona body from resolver.",
    )
    equip = DispatchEquip(
        persona="coder",
        base_loadout="coder",
        skill_slots_execute=("superpowers/systematic-debugging",),
        skill_slots_review=(),
        level_up_generation=1,
        compose_body="# Equip compose\n\nSystematic Debugging\n\nRoot cause first.",
    )
    task = FleetTask(goal="Fix failing test", persona="coder", equip=equip)

    prompt = _build_execute_prompt(persona, task)

    assert "Static persona body from resolver." not in prompt
    assert "Systematic Debugging" in prompt
    assert "Fix failing test" in prompt


def test_execute_prompt_includes_persona_modifiers_and_closing() -> None:
    persona = Persona(
        name="coder",
        prompt_path=Path("coder.md"),
        allowed_tools=[],
        capabilities={},
        body="Coder body.",
        allowed_paths=("src/",),
        extra_instructions="Prefer small diffs.",
    )
    task = FleetTask(
        goal="Implement feature",
        context="See design doc.",
        persona="coder",
    )

    prompt = _build_execute_prompt(persona, task)

    assert prompt.index("# Persona") < prompt.index("# Additional Instructions")
    assert prompt.index("# Additional Instructions") < prompt.index("# Scope:")
    assert prompt.index("# Scope:") < prompt.index("# Task")
    assert prompt.index("# Task") < prompt.index("# Context")
    assert prompt.index("# Context") < prompt.index("Execute this task in the workspace")
    assert "Prefer small diffs." in prompt
    assert "only modify paths matching: src/" in prompt
    assert "See design doc." in prompt
