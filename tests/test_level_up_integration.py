# ruff: noqa: ARG001
"""Integration: equip → compose → experience recording."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.hooks import FleetTask
from agent_fleet.level_up import paths as level_up_paths
from agent_fleet.orchestration.equip import resolve_dispatch_equip
from agent_fleet.personas import YamlPersonaResolver, read_persona_body
from agent_fleet.repo import load_repo_config


@pytest.fixture
def level_up_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "level_up"
    monkeypatch.setattr(level_up_paths, "LEVEL_UP_ROOT", root)
    return root


@pytest.fixture
def fleet_config() -> FleetConfig:
    root = Path(__file__).resolve().parent.parent
    return load_fleet_config(root / "fleet.example.yaml")


def test_equip_compose_and_journal(
    fleet_config: FleetConfig,
    level_up_root: Path,
    tmp_path: Path,
) -> None:
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text("name: integration-repo\n", encoding="utf-8")
    repo = load_repo_config(repo_yaml)
    fleet_config.repo_config = repo

    task = FleetTask(
        goal="Add test for level-up integration",
        persona="coder",
        workspace=str(tmp_path),
        pipeline="code_review",
    )
    equip = resolve_dispatch_equip(
        task,
        fleet_config,
        repo,
        run_id="integration-run-1",
    )
    assert equip.skill_slots_review or equip.base_loadout == "coder"

    resolver = YamlPersonaResolver(fleet_config)
    persona = resolver.load("coder")
    body = read_persona_body(persona)
    assert "Role" in body or "coding" in body.lower()

    journal_path = level_up_paths.persona_dir("integration-repo", "coder") / "journal.jsonl"
    assert journal_path.is_file()
    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line)["event"] for line in lines if line]
    assert "equip.loadout" in events
    assert "equip.compose" in events
