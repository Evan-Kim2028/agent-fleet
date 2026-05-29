"""Tests pinning the cli-for-agents skill vendoring and reviewer loadout wiring."""

from __future__ import annotations

from agent_fleet.personas import load_loadout
from agent_fleet.skills_lib import (
    base_kit_dir,
    base_kit_skill_dirs,
    load_skill_text,
    resolve_skill_path,
    skill_exists_in_base_kit,
)


def test_resolve_skill_path_returns_skill_md() -> None:
    dirs = base_kit_skill_dirs()
    path = resolve_skill_path("cli-for-agents", dirs)
    assert path is not None
    assert path.name == "SKILL.md"


def test_skill_exists_in_base_kit() -> None:
    assert skill_exists_in_base_kit("cli-for-agents") is True


def test_skill_text_contains_non_interactive() -> None:
    dirs = base_kit_skill_dirs()
    text = load_skill_text("cli-for-agents", dirs)
    assert "Non-interactive" in text


def test_skill_text_contains_help_flag() -> None:
    dirs = base_kit_skill_dirs()
    text = load_skill_text("cli-for-agents", dirs)
    assert "--help" in text


def test_skill_text_contains_dry_run() -> None:
    dirs = base_kit_skill_dirs()
    text = load_skill_text("cli-for-agents", dirs)
    assert "dry-run" in text


def test_attribution_file_mentions_cursor_and_mit() -> None:
    attr = base_kit_dir() / "cli-for-agents" / "ATTRIBUTION.md"
    assert attr.exists()
    text = attr.read_text(encoding="utf-8")
    assert "cursor/plugins" in text
    assert "MIT" in text


def test_license_file_contains_mit_license() -> None:
    lic = base_kit_dir() / "cli-for-agents" / "LICENSE"
    assert lic.exists()
    text = lic.read_text(encoding="utf-8")
    assert "MIT License" in text


def test_reviewer_loadout_review_contains_cli_for_agents() -> None:
    loadout = load_loadout("reviewer")
    assert loadout is not None
    review = loadout["pipeline_skills"]["code_review"]["review"]
    assert "cli-for-agents" in review


def test_reviewer_loadout_review_first_two_entries_unchanged() -> None:
    loadout = load_loadout("reviewer")
    assert loadout is not None
    review = loadout["pipeline_skills"]["code_review"]["review"]
    assert review[:2] == ["pstack/unslop", "cursor-team-kit/deslop"]


def test_reviewer_loadout_cli_for_agents_is_appended_not_reordered() -> None:
    loadout = load_loadout("reviewer")
    assert loadout is not None
    review = loadout["pipeline_skills"]["code_review"]["review"]
    idx = review.index("cli-for-agents")
    assert idx > 1
