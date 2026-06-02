"""Regression tests: configurable complexity tiers and skill sets via fleet.yaml.

These tests verify that:
- YAML complexity_tiers overrides change the effective RuntimeConfig.
- YAML skills overrides change the effective minimal_core and pr_loop skill sets.
- Absent config yields the current Python defaults unchanged.
"""

from __future__ import annotations

from pathlib import Path

from agent_fleet.complexity import _RUNTIME_MAP, RuntimeConfig, derive_runtime
from agent_fleet.config import load_fleet_config
from agent_fleet.skills_lib import (
    MINIMAL_EXECUTE_SKILL_CORE,
    PR_LOOP_EXECUTE_SKILLS,
    effective_minimal_core,
    effective_pr_loop_skills,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# derive_runtime: tier_overrides param
# ---------------------------------------------------------------------------


def test_derive_runtime_no_overrides_uses_defaults() -> None:
    """tier_overrides=None preserves the Python constant defaults."""
    for level in ("LOW", "MED", "HIGH"):
        assert derive_runtime(level) == _RUNTIME_MAP[level]
        assert derive_runtime(level, tier_overrides=None) == _RUNTIME_MAP[level]


def test_derive_runtime_partial_override_low_token_ceiling() -> None:
    """A single field override merges over the default; other fields unchanged."""
    overrides = {"LOW": {"token_ceiling": 2_000_000}}
    rt = derive_runtime("LOW", tier_overrides=overrides)
    default = _RUNTIME_MAP["LOW"]
    assert rt.token_ceiling == 2_000_000
    assert rt.pipeline == default.pipeline
    assert rt.retries == default.retries
    assert rt.loadout_size == default.loadout_size


def test_derive_runtime_override_different_tier_unchanged() -> None:
    """Override for MED does not affect LOW or HIGH."""
    overrides = {"MED": {"retries": 3}}
    assert derive_runtime("LOW", tier_overrides=overrides) == _RUNTIME_MAP["LOW"]
    assert derive_runtime("HIGH", tier_overrides=overrides) == _RUNTIME_MAP["HIGH"]
    rt_med = derive_runtime("MED", tier_overrides=overrides)
    assert rt_med.retries == 3


def test_derive_runtime_all_fields_override() -> None:
    """All RuntimeConfig fields can be overridden for a single tier."""
    overrides = {
        "HIGH": {
            "pipeline": "simple",
            "retries": 5,
            "token_ceiling": 99_000_000,
            "loadout_size": "minimal",
        }
    }
    rt = derive_runtime("HIGH", tier_overrides=overrides)
    assert rt == RuntimeConfig(
        pipeline="simple",
        retries=5,
        token_ceiling=99_000_000,
        loadout_size="minimal",
    )


def test_derive_runtime_empty_overrides_dict() -> None:
    """Empty overrides dict is equivalent to None — uses Python defaults."""
    assert derive_runtime("MED", tier_overrides={}) == _RUNTIME_MAP["MED"]


# ---------------------------------------------------------------------------
# load_fleet_config: complexity_tiers and skills parsed from YAML
# ---------------------------------------------------------------------------


def test_load_fleet_config_no_tiers_empty_by_default(tmp_path: Path) -> None:
    """A config with no complexity_tiers block yields empty complexity_tiers."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text("default_model: composer-2.5\n", encoding="utf-8")
    fc = load_fleet_config(cfg_file)
    assert fc.complexity_tiers == {}


def test_load_fleet_config_no_skills_empty_by_default(tmp_path: Path) -> None:
    """A config with no skills block yields empty skill_overrides."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text("default_model: composer-2.5\n", encoding="utf-8")
    fc = load_fleet_config(cfg_file)
    assert fc.skill_overrides == {}


def test_load_fleet_config_complexity_tiers_parsed(tmp_path: Path) -> None:
    """complexity_tiers block is parsed and stored on FleetConfig."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(
        "complexity_tiers:\n"
        "  LOW:\n"
        "    token_ceiling: 2000000\n"
        "    retries: 2\n"
        "  MED:\n"
        "    pipeline: simple\n",
        encoding="utf-8",
    )
    fc = load_fleet_config(cfg_file)
    assert fc.complexity_tiers["LOW"]["token_ceiling"] == 2_000_000
    assert fc.complexity_tiers["LOW"]["retries"] == 2
    assert fc.complexity_tiers["MED"]["pipeline"] == "simple"
    assert "HIGH" not in fc.complexity_tiers


def test_load_fleet_config_skills_parsed(tmp_path: Path) -> None:
    """skills block minimal_core and pr_loop are parsed."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(
        "skills:\n"
        "  minimal_core:\n"
        "    - pstack/tdd\n"
        "    - custom/skill\n"
        "  pr_loop:\n"
        "    - cursor-team-kit/fix-ci\n",
        encoding="utf-8",
    )
    fc = load_fleet_config(cfg_file)
    assert fc.skill_overrides["minimal_core"] == ["pstack/tdd", "custom/skill"]
    assert fc.skill_overrides["pr_loop"] == ["cursor-team-kit/fix-ci"]


def test_load_fleet_config_example_yaml_no_tiers() -> None:
    """fleet.example.yaml has no complexity_tiers/skills — existing defaults unchanged."""
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    assert fc.complexity_tiers == {}
    assert fc.skill_overrides == {}
    # Existing behavior: derive_runtime still returns Python defaults.
    assert derive_runtime("LOW", tier_overrides=fc.complexity_tiers or None) == _RUNTIME_MAP["LOW"]


# ---------------------------------------------------------------------------
# effective_minimal_core / effective_pr_loop_skills
# ---------------------------------------------------------------------------


def test_effective_minimal_core_no_config_returns_default() -> None:
    """Without a config, the Python constant is returned."""
    assert effective_minimal_core(None) is MINIMAL_EXECUTE_SKILL_CORE
    assert effective_minimal_core() is MINIMAL_EXECUTE_SKILL_CORE


def test_effective_minimal_core_empty_overrides_returns_default() -> None:
    """Config with no skills block returns the constant."""
    fc = load_fleet_config(ROOT / "fleet.example.yaml")
    assert effective_minimal_core(fc) is MINIMAL_EXECUTE_SKILL_CORE


def test_effective_minimal_core_config_override(tmp_path: Path) -> None:
    """Config minimal_core list replaces the constant."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(
        "skills:\n  minimal_core:\n    - pstack/tdd\n    - custom/extra\n",
        encoding="utf-8",
    )
    fc = load_fleet_config(cfg_file)
    core = effective_minimal_core(fc)
    assert core == frozenset({"pstack/tdd", "custom/extra"})
    assert core is not MINIMAL_EXECUTE_SKILL_CORE


def test_effective_pr_loop_skills_no_config_returns_default() -> None:
    """Without a config, the Python constant is returned."""
    assert effective_pr_loop_skills(None) is PR_LOOP_EXECUTE_SKILLS
    assert effective_pr_loop_skills() is PR_LOOP_EXECUTE_SKILLS


def test_effective_pr_loop_skills_config_override(tmp_path: Path) -> None:
    """Config pr_loop list replaces the constant."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(
        "skills:\n  pr_loop:\n    - cursor-team-kit/fix-ci\n",
        encoding="utf-8",
    )
    fc = load_fleet_config(cfg_file)
    skills = effective_pr_loop_skills(fc)
    assert skills == ("cursor-team-kit/fix-ci",)
    assert skills is not PR_LOOP_EXECUTE_SKILLS


# ---------------------------------------------------------------------------
# Integration: YAML-driven tier override flows end-to-end through derive_runtime
# ---------------------------------------------------------------------------


def test_yaml_tier_override_changes_effective_runtime(tmp_path: Path) -> None:
    """A complexity_tiers override in fleet.yaml changes what derive_runtime returns."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(
        "complexity_tiers:\n  LOW:\n    token_ceiling: 500000\n    retries: 3\n",
        encoding="utf-8",
    )
    fc = load_fleet_config(cfg_file)
    rt = derive_runtime("LOW", tier_overrides=fc.complexity_tiers or None)
    assert rt.token_ceiling == 500_000
    assert rt.retries == 3
    # Fields not in override stay at Python defaults.
    assert rt.pipeline == _RUNTIME_MAP["LOW"].pipeline
    assert rt.loadout_size == _RUNTIME_MAP["LOW"].loadout_size


def test_absent_yaml_tier_override_preserves_defaults(tmp_path: Path) -> None:
    """When complexity_tiers is absent, all tiers stay at Python defaults."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text("", encoding="utf-8")
    fc = load_fleet_config(cfg_file)
    for level in ("LOW", "MED", "HIGH"):
        rt = derive_runtime(level, tier_overrides=fc.complexity_tiers or None)
        assert rt == _RUNTIME_MAP[level]


# ---------------------------------------------------------------------------
# Parse helpers: invalid/unknown keys silently dropped
# ---------------------------------------------------------------------------


def test_parse_complexity_tiers_unknown_tier_ignored(tmp_path: Path) -> None:
    """Unknown tier names are silently dropped."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(
        "complexity_tiers:\n  EXTREME:\n    retries: 10\n  LOW:\n    retries: 2\n",
        encoding="utf-8",
    )
    fc = load_fleet_config(cfg_file)
    assert "EXTREME" not in fc.complexity_tiers
    assert fc.complexity_tiers["LOW"]["retries"] == 2


def test_parse_complexity_tiers_unknown_field_ignored(tmp_path: Path) -> None:
    """Unknown field names within a tier are silently dropped."""
    cfg_file = tmp_path / "fleet.yaml"
    cfg_file.write_text(
        "complexity_tiers:\n  MED:\n    token_ceiling: 3000000\n    unknown_field: oops\n",
        encoding="utf-8",
    )
    fc = load_fleet_config(cfg_file)
    assert "unknown_field" not in fc.complexity_tiers.get("MED", {})
    assert fc.complexity_tiers["MED"]["token_ceiling"] == 3_000_000
