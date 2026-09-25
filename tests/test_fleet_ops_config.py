"""Per-operator config: parsing, {lane} expansion, baseline_skip_hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.config import (
    DEFAULT_PUSH_BRANCH,
    DEFAULT_STALL_MINUTES,
    effective_stall_minutes,
    expand_template,
    load_fleet_ops_config,
    load_fleet_ops_config_from_repo,
)

if TYPE_CHECKING:
    from pathlib import Path

FULL = {
    "fleet_ops": {
        "base_branch": "trunk",
        "stall_minutes": 35,
        "baseline_skip_hooks": ["ruff-format", "pyright"],
        "operators": {
            "documents-0e": {
                "engine": "cmd",
                "push_branch": "fb/{lane}",
                "task_file": "prompts/{lane}.task.md",
                "on_approved": "echo approved-0e",
            },
            "documents-1d": {
                "engine": "devin",
                "push_branch": "ops/{lane}",
            },
        },
    }
}


def test_parses_operators_and_globals() -> None:
    cfg = load_fleet_ops_config(FULL)
    assert cfg is not None
    assert cfg.base_branch == "trunk"
    assert cfg.stall_minutes == 35
    assert cfg.baseline_skip_hooks == ("ruff-format", "pyright")
    assert sorted(cfg.operators) == ["documents-0e", "documents-1d"]


def test_missing_section_returns_none() -> None:
    assert load_fleet_ops_config({}) is None
    assert load_fleet_ops_config(None) is None
    assert load_fleet_ops_config({"fleet_ops": False}) is None
    assert load_fleet_ops_config({"fleet_ops": []}) is None


def test_lane_placeholder_expands_per_operator() -> None:
    cfg = load_fleet_ops_config(FULL)
    zero_e = cfg.operator("documents-0e")
    one_d = cfg.operator("documents-1d")
    assert zero_e.branch_for("stampinplace") == "fb/stampinplace"
    assert one_d.branch_for("stampinplace") == "ops/stampinplace"
    assert zero_e.task_file_for("stampinplace") == "prompts/stampinplace.task.md"
    assert one_d.task_file_for("stampinplace") is None


def test_expand_template_does_not_choke_on_stray_braces() -> None:
    """Operator input comes from YAML; a brace in it must not raise."""
    assert expand_template("a{lane}b{lane}", lane="x") == "axbx"
    assert expand_template("literal {other} stays", lane="x") == "literal {other} stays"
    assert expand_template("{operator}/{lane}", lane="l", operator="op") == "op/l"


def test_skip_env_lists_only_named_hooks() -> None:
    cfg = load_fleet_ops_config(FULL)
    assert cfg.skip_env() == {"SKIP": "ruff-format,pyright"}


def test_skip_env_empty_when_no_baseline_hooks() -> None:
    cfg = load_fleet_ops_config({"fleet_ops": {"operators": {}}})
    assert cfg.skip_env() == {}


def test_defaults_applied_when_section_is_sparse() -> None:
    cfg = load_fleet_ops_config({"fleet_ops": {"operators": {"op": {}}}})
    assert cfg is not None
    spec = cfg.operator("op")
    assert spec.engine == "cmd"
    assert spec.push_branch == DEFAULT_PUSH_BRANCH
    assert spec.branch_for("l") == "fb/l"
    assert cfg.stall_minutes == DEFAULT_STALL_MINUTES


def test_operator_stall_minutes_override_global() -> None:
    raw = {
        "fleet_ops": {
            "stall_minutes": 20,
            "operators": {"slow": {"stall_minutes": 45}, "normal": {}},
        }
    }
    cfg = load_fleet_ops_config(raw)
    assert effective_stall_minutes(cfg, "slow") == 45
    assert effective_stall_minutes(cfg, "normal") == 20
    assert effective_stall_minutes(cfg, "unknown-operator") == 20


def test_malformed_operator_entries_are_skipped() -> None:
    cfg = load_fleet_ops_config(
        {"fleet_ops": {"operators": {"good": {"engine": "cmd"}, "bad": "not-a-dict", "": {}}}}
    )
    assert sorted(cfg.operators) == ["good"]


def test_load_from_repo_yaml_file(tmp_path: Path) -> None:
    (tmp_path / ".agent-fleet.yaml").write_text(
        "fleet_ops:\n"
        "  baseline_skip_hooks: [api-consumer-docs-check]\n"
        "  operators:\n"
        "    documents-0e:\n"
        "      engine: cmd\n",
        encoding="utf-8",
    )
    cfg = load_fleet_ops_config_from_repo(tmp_path)
    assert cfg is not None
    assert cfg.baseline_skip_hooks == ("api-consumer-docs-check",)
    assert cfg.operator("documents-0e").engine == "cmd"


def test_load_from_repo_without_section(tmp_path: Path) -> None:
    (tmp_path / ".agent-fleet.yaml").write_text("name: x\n", encoding="utf-8")
    assert load_fleet_ops_config_from_repo(tmp_path) is None


def test_load_from_repo_without_config_file(tmp_path: Path) -> None:
    assert load_fleet_ops_config_from_repo(tmp_path) is None
