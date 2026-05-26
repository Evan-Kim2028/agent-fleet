"""Tests for bundled skills and quality review pass."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.pr_review.analyzer import passes_for_files
from agent_fleet.pr_review.config import PrReviewConfig
from agent_fleet.skills_lib import (
    DEFAULT_QUALITY_REVIEW_SKILL,
    bundled_skill_dirs,
    load_skill_text,
    resolve_skill_path,
)


def test_canonical_thermo_nuclear_skill_loads_from_base_kit() -> None:
    dirs = bundled_skill_dirs()
    assert dirs
    path = resolve_skill_path(DEFAULT_QUALITY_REVIEW_SKILL, dirs)
    assert path is not None
    assert "cursor-team-kit/thermo-nuclear-code-quality-review" in str(path)
    text = load_skill_text(DEFAULT_QUALITY_REVIEW_SKILL, dirs)
    assert "Thermo-Nuclear Code Quality Review" in text
    assert "code judo" in text.lower()


def test_legacy_thermo_nuclear_id_aliases_to_base_kit() -> None:
    dirs = bundled_skill_dirs()
    legacy_path = resolve_skill_path("thermo-nuclear-code-quality-review", dirs)
    canonical_path = resolve_skill_path(DEFAULT_QUALITY_REVIEW_SKILL, dirs)
    assert legacy_path == canonical_path


def test_bundled_skills_dir_has_no_duplicate_thermo_nuclear() -> None:
    bundled_dir = Path(__file__).resolve().parent.parent / "agent_fleet" / "skills"
    duplicate = bundled_dir / "thermo-nuclear-code-quality-review" / "SKILL.md"
    assert not duplicate.is_file()


def test_quality_pass_enabled_by_default() -> None:
    config = PrReviewConfig()
    modes = passes_for_files(["api/main.py"], config)
    assert "quality" in modes
    assert "backend-security" in modes


def test_quality_pass_can_disable() -> None:
    config = PrReviewConfig(quality_review_enabled=False)
    modes = passes_for_files(["api/main.py"], config)
    assert "quality" not in modes


def test_load_pr_review_quality_from_yaml() -> None:
    from agent_fleet.pr_review.config import load_pr_review_config

    cfg = load_pr_review_config(
        Path("/tmp"),
        {
            "pr_review": {
                "quality_review": {
                    "enabled": True,
                    "skill": DEFAULT_QUALITY_REVIEW_SKILL,
                }
            }
        },
    )
    assert cfg is not None
    assert cfg.quality_review_enabled is True
    assert cfg.quality_review_skill == DEFAULT_QUALITY_REVIEW_SKILL


def test_load_pr_review_quality_legacy_yaml_id_still_resolves() -> None:
    from agent_fleet.pr_review.config import load_pr_review_config

    cfg = load_pr_review_config(
        Path("/tmp"),
        {
            "pr_review": {
                "quality_review": {
                    "enabled": True,
                    "skill": "thermo-nuclear-code-quality-review",
                }
            }
        },
    )
    assert cfg is not None
    assert cfg.quality_review_skill == "thermo-nuclear-code-quality-review"
    text = load_skill_text(cfg.quality_review_skill, bundled_skill_dirs())
    assert "Thermo-Nuclear Code Quality Review" in text
