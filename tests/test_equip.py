# ruff: noqa: TC002
"""Tests for orchestration equip resolution and dispatcher integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.task_spec import DecompositionDecision, RiskTier, Scope, TaskSpec
from agent_fleet.hooks import FleetTask
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.level_up.paths import LEVEL_UP_ROOT, persona_dir
from agent_fleet.orchestration.decompose import child_tasks_from_task_spec
from agent_fleet.orchestration.equip import resolve_dispatch_equip
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import load_repo_config
from agent_fleet.skills_lib import (
    MINIMAL_EXECUTE_SKILL_CORE,
    PR_LOOP_EXECUTE_SKILLS,
    SYSTEMATIC_DEBUGGING_SKILL,
    load_loadout,
    loadout_execute_skill_ids,
)

ROOT = Path(__file__).resolve().parent.parent


def _patch_level_up_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", root)


def test_load_coder_loadout() -> None:
    loadout = load_loadout("coder")
    execute = loadout_execute_skill_ids(loadout)
    assert "pstack/tdd" in execute


def test_resolve_dispatch_equip_missing_loadout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: md-only\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="Slim repo", persona="frontend", workspace=str(tmp_path))

    equip = resolve_dispatch_equip(task, fleet_config, repo, run_id="md-only-run")

    assert equip.base_loadout == "frontend"
    assert equip.skill_slots_execute == ()
    assert equip.skill_slots_review == ()
    journal_path = persona_dir("md-only", "frontend") / "journal.jsonl"
    assert journal_path.is_file()


def test_resolve_dispatch_equip_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: equip-demo\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="Fix bug", persona="coder", workspace=str(tmp_path))

    equip = resolve_dispatch_equip(task, fleet_config, repo, run_id="run-1")

    assert isinstance(equip, DispatchEquip)
    assert equip.persona == "coder"
    assert equip.base_loadout == "coder"
    assert "pstack/tdd" in equip.skill_slots_execute
    assert equip.parent_run_id is None
    assert "TDD Bug Fix" in equip.compose_body

    journal_path = persona_dir("equip-demo", "coder") / "journal.jsonl"
    assert journal_path.is_file()
    events = [
        json.loads(line)["event"] for line in journal_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["equip.loadout", "equip.compose"]


def test_verify_failed_adds_systematic_debugging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    level_up_root = tmp_path / "level_up"
    _patch_level_up_root(monkeypatch, level_up_root)

    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: verify-fail\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    exp_dir = persona_dir("verify-fail", "coder")
    exp_dir.mkdir(parents=True)
    (exp_dir / "experience.jsonl").write_text(
        json.dumps({"status": "verify_failed", "goal": "prior task"}) + "\n",
        encoding="utf-8",
    )

    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="Retry fix", persona="coder", workspace=str(tmp_path))
    equip = resolve_dispatch_equip(task, fleet_config, repo)

    assert SYSTEMATIC_DEBUGGING_SKILL in equip.skill_slots_execute
    assert "# Why" in equip.compose_body


def test_verify_failed_skips_missing_base_kit_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    monkeypatch.setattr(
        "agent_fleet.orchestration.equip.skill_exists_in_base_kit", lambda _skill_id: False
    )

    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: no-skill\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    exp_dir = persona_dir("no-skill", "coder")
    exp_dir.mkdir(parents=True)
    (exp_dir / "experience.jsonl").write_text(
        json.dumps({"status": "verify_failed"}) + "\n",
        encoding="utf-8",
    )

    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="Retry fix", persona="coder", workspace=str(tmp_path))
    equip = resolve_dispatch_equip(task, fleet_config, repo)

    assert SYSTEMATIC_DEBUGGING_SKILL not in equip.skill_slots_execute


def test_pr_loop_adds_ci_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(
        "name: pr-loop-repo\npr_loop:\n  enabled: true\n",
        encoding="utf-8",
    )
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="Fix CI", persona="coder", workspace=str(tmp_path))
    equip = resolve_dispatch_equip(task, fleet_config, repo)

    for skill_id in PR_LOOP_EXECUTE_SKILLS:
        assert skill_id in equip.skill_slots_execute


def test_child_tasks_carry_parent_run_id() -> None:
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    parent = FleetTask(goal="Parent", persona="coder")
    task_spec = TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.DECOMPOSE,
        decomposition_reason="split work",
        child_issues_proposed=[{"title": "Child A", "body": "work", "persona": "coder"}],
        scope=Scope(allowed_paths=[], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=[],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
    )

    children = child_tasks_from_task_spec(
        task_spec,
        parent_task=parent,
        child_pipeline="simple",
        persona_resolver=resolver,
        fallback_persona="coder",
        parent_run_id="parent-run-42",
    )

    assert len(children) == 1
    assert children[0].equip is not None
    assert children[0].equip.parent_run_id == "parent-run-42"


def test_resolve_preserves_parent_run_id_from_task_equip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: child-equip\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    stub_equip = DispatchEquip(
        persona="coder",
        base_loadout="coder",
        skill_slots_execute=(),
        skill_slots_review=(),
        level_up_generation=0,
        parent_run_id="parent-run-42",
    )
    task = FleetTask(goal="Child", persona="coder", workspace=str(tmp_path), equip=stub_equip)

    equip = resolve_dispatch_equip(task, fleet_config, repo, run_id="child-run")

    assert equip.parent_run_id == "parent-run-42"


def test_level_up_root_default() -> None:
    assert LEVEL_UP_ROOT.name == "level_up"


def test_minimal_loadout_size_reduces_compose_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: minimal-test\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="Small fix", persona="coder", workspace=str(tmp_path))

    minimal_equip = resolve_dispatch_equip(task, fleet_config, repo, loadout_size="minimal")
    full_equip = resolve_dispatch_equip(task, fleet_config, repo, loadout_size="full")

    # minimal must produce a shorter prompt than full
    assert len(minimal_equip.compose_body) < len(full_equip.compose_body)

    # a core skill must appear in minimal
    assert "pstack/tdd" in minimal_equip.skill_slots_execute or "TDD" in minimal_equip.compose_body

    # a dropped skill must NOT appear in minimal compose body
    assert "principle-prove-it-works" not in minimal_equip.compose_body

    # None and "full" must be byte-identical (no-op path)
    none_equip = resolve_dispatch_equip(task, fleet_config, repo, loadout_size=None)
    assert none_equip.compose_body == full_equip.compose_body

    # standard must also be byte-identical to full/None
    standard_equip = resolve_dispatch_equip(task, fleet_config, repo, loadout_size="standard")
    assert standard_equip.compose_body == full_equip.compose_body

    # verify the constant itself contains only the expected 4 skills
    assert {
        "pstack/tdd",
        "cursor-team-kit/verify-this",
        "pstack/figure-it-out",
        "pstack/principle-fix-root-causes",
    } == MINIMAL_EXECUTE_SKILL_CORE
