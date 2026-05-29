"""Unit 6 adversarial self-review gate: prove the gate is equipped."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.phases import _review_skills_from_slots
from agent_fleet.skills_lib import load_loadout, loadout_review_skill_ids, skill_exists_in_base_kit

_BASE_KIT = Path(__file__).resolve().parent.parent / "agent_fleet" / "base-kit"
_ADVERSARIAL_SKILLS: tuple[str, str] = (
    "agent-skills/doubt-driven-development",
    "agent-skills/code-review-and-quality",
)


@pytest.mark.parametrize("persona", ["coder", "pr-analyzer", "reviewer"])
def test_review_slots_have_adversarial_skills(persona: str) -> None:
    loadout = load_loadout(persona)
    review_ids = loadout_review_skill_ids(loadout)
    for skill_id in _ADVERSARIAL_SKILLS:
        assert skill_id in review_ids, (
            f"Persona {persona!r} review slots missing {skill_id!r}; got {review_ids}"
        )


def test_review_prompt_append_injects_skill_content() -> None:
    result = _review_skills_from_slots(_ADVERSARIAL_SKILLS)

    assert result, "_review_skills_from_slots returned empty string"

    ddd_path = _BASE_KIT / "agent-skills" / "doubt-driven-development" / "SKILL.md"
    crq_path = _BASE_KIT / "agent-skills" / "code-review-and-quality" / "SKILL.md"

    ddd_text = ddd_path.read_text(encoding="utf-8")
    crq_text = crq_path.read_text(encoding="utf-8")

    # Pick a stable substring from each SKILL.md that is unlikely to change.
    ddd_marker = "Doubt-Driven Development"
    crq_marker = "Code Review and Quality"

    assert ddd_marker in ddd_text, f"Expected marker not found in {ddd_path}"
    assert crq_marker in crq_text, f"Expected marker not found in {crq_path}"

    assert ddd_marker in result, (
        f"Review prompt missing doubt-driven-development content (looked for {ddd_marker!r})"
    )
    assert crq_marker in result, (
        f"Review prompt missing code-review-and-quality content (looked for {crq_marker!r})"
    )


def test_adversarial_skills_resolve() -> None:
    for skill_id in _ADVERSARIAL_SKILLS:
        assert skill_exists_in_base_kit(skill_id), (
            f"skill_exists_in_base_kit returned False for {skill_id!r}"
        )
