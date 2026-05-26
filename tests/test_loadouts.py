"""Tests for persona loadouts and base-kit skill composition."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.personas import load_loadout
from agent_fleet.skills_lib import (
    base_kit_skill_dirs,
    compose_persona_body,
    load_skill_text,
    loadout_execute_skill_ids,
    loadout_review_skill_ids,
    resolve_skill_path,
)

ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR = ROOT / "agent_fleet" / "personas"


def bundled_loadout_names() -> list[str]:
    return sorted(path.stem.replace(".loadout", "") for path in PERSONAS_DIR.glob("*.loadout.yaml"))


@pytest.mark.parametrize("persona", bundled_loadout_names())
def test_loadout_skill_ids_resolve_in_base_kit(persona: str) -> None:
    loadout = load_loadout(persona)
    assert loadout is not None
    dirs = base_kit_skill_dirs()
    skill_ids = [
        *loadout_execute_skill_ids(loadout),
        *loadout_review_skill_ids(loadout),
    ]
    assert skill_ids, persona
    for skill_id in skill_ids:
        path = resolve_skill_path(skill_id, dirs)
        assert path is not None, f"{persona}: {skill_id}"
        assert path.name == "SKILL.md"


def test_unslop_loads_from_base_kit() -> None:
    dirs = base_kit_skill_dirs()
    assert dirs
    path = resolve_skill_path("pstack/unslop", dirs)
    assert path is not None
    assert path.name == "SKILL.md"
    text = load_skill_text("pstack/unslop", dirs)
    assert "slop" in text.lower()


def test_reviewer_loadout_lists_unslop_for_review() -> None:
    loadout = load_loadout("reviewer")
    assert loadout is not None
    review_skills = loadout["pipeline_skills"]["code_review"]["review"]
    assert review_skills == ["pstack/unslop", "cursor-team-kit/deslop"]


def test_compose_persona_body_includes_sections() -> None:
    loadout = load_loadout("coder")
    assert loadout is not None
    stub = (PERSONAS_DIR / "coder.md").read_text(encoding="utf-8")
    body = compose_persona_body(
        loadout,
        fleet_overlay="- Prefer ruff before finishing.",
        repo_overlay="- Run pytest -q before claiming done.",
        stub_text=stub,
        skill_dirs=base_kit_skill_dirs(),
        level_up_generation=2,
    )
    assert "General-purpose coding agent" in body
    assert "# Fleet learned" in body
    assert "ruff before finishing" in body
    assert "# Repo learned (generation 2)" in body
    assert "pytest -q" in body


def test_coder_loadout_references_base_kit_skill_files() -> None:
    loadout = load_loadout("coder")
    assert loadout is not None
    dirs = base_kit_skill_dirs()
    execute_ids = loadout["skills"]["execute"]
    for skill_id in execute_ids:
        path = resolve_skill_path(skill_id, dirs)
        assert path is not None, skill_id
        assert path.name == "SKILL.md"
