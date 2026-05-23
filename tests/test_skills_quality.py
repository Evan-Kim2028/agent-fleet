"""Tests for bundled skills and quality review pass."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.pr_review.analyzer import passes_for_files
from agent_fleet.pr_review.config import PrReviewConfig
from agent_fleet.skills_lib import bundled_skill_dirs, load_skill_text


def test_bundled_thermo_nuclear_skill_loads() -> None:
    dirs = bundled_skill_dirs()
    assert dirs
    text = load_skill_text("thermo-nuclear-code-quality-review", dirs)
    assert "Thermo-Nuclear Code Quality Review" in text
    assert "code judo" in text.lower()


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
                    "skill": "thermo-nuclear-code-quality-review",
                }
            }
        },
    )
    assert cfg is not None
    assert cfg.quality_review_enabled is True
    assert cfg.quality_review_skill == "thermo-nuclear-code-quality-review"
