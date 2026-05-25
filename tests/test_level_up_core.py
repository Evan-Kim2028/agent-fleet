"""Tests for agent_fleet.level_up core package."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_fleet.level_up import (
    LevelUpConfig,
    LevelUpRule,
    append_experience,
    append_journal,
    compose_overlay_prompt,
    fleet_persona_dir,
    load_overlay,
    persona_dir,
    repo_key,
)
from agent_fleet.level_up import journal as level_up_journal
from agent_fleet.level_up import paths as level_up_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def level_up_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "level_up"
    index = tmp_path / "journal" / "index.jsonl"
    monkeypatch.setattr(level_up_paths, "LEVEL_UP_ROOT", root)
    monkeypatch.setattr(level_up_journal, "JOURNAL_INDEX_PATH", index)
    monkeypatch.setattr(level_up_paths, "JOURNAL_INDEX_PATH", index)
    return root


def test_repo_key_uses_name_when_provided() -> None:
    assert repo_key("agent-fleet", "/tmp/other") == "agent-fleet"


def test_repo_key_falls_back_to_repo_root_name(tmp_path: Path) -> None:
    repo = tmp_path / "My Project"
    repo.mkdir()
    assert repo_key(None, repo) == "My Project"
    assert repo_key("", repo) == "My Project"


def test_persona_and_fleet_dirs(level_up_root: Path) -> None:
    assert persona_dir("agent-fleet", "coder") == level_up_root / "agent-fleet" / "coder"
    assert fleet_persona_dir("reviewer") == level_up_root / "_fleet" / "reviewer"


def test_level_up_config_defaults() -> None:
    config = LevelUpConfig.from_dict({})
    assert config.train is True
    assert config.contribute_to_fleet is True
    assert config.journal_task_summaries is True


def test_level_up_config_from_dict() -> None:
    config = LevelUpConfig.from_dict(
        {
            "train": False,
            "contribute_to_fleet": False,
            "journal_task_summaries": False,
        }
    )
    assert config.train is False
    assert config.contribute_to_fleet is False
    assert config.journal_task_summaries is False


def test_append_journal_creates_persona_journal(level_up_root: Path) -> None:
    append_journal(
        "run.complete",
        "agent-fleet",
        "coder",
        run_id="run-1",
        data={"status": "success"},
    )

    journal_path = level_up_root / "agent-fleet" / "coder" / "journal.jsonl"
    assert journal_path.is_file()
    record = json.loads(journal_path.read_text(encoding="utf-8").strip())
    assert record["event"] == "run.complete"
    assert record["repo_key"] == "agent-fleet"
    assert record["persona"] == "coder"
    assert record["run_id"] == "run-1"
    assert record["data"]["status"] == "success"


def test_append_journal_writes_global_index(
    level_up_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = level_up_root.parent / "journal" / "index.jsonl"
    monkeypatch.setattr(level_up_journal, "JOURNAL_INDEX_PATH", index_path)

    append_journal("experience.appended", "agent-fleet", "coder")

    assert index_path.is_file()
    record = json.loads(index_path.read_text(encoding="utf-8").strip())
    assert record["event"] == "experience.appended"


def test_append_experience_creates_file(level_up_root: Path) -> None:
    entry = append_experience(
        repo_key="agent-fleet",
        persona="coder",
        source="pr_loop",
        weight=2.0,
        pr_loop_round=2,
        status="verify_failed",
        goal="Fix tests",
        review_verdict="changes_requested",
        equip_snapshot={"skill_slots_execute": ["superpowers/tdd"]},
        changed_files=["agent_fleet/dispatcher.py"],
        run_id="run-42",
    )

    path = level_up_root / "agent-fleet" / "coder" / "experience.jsonl"
    assert path.is_file()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["source"] == "pr_loop"
    assert record["weight"] == 2.0
    assert record["pr_loop_round"] == 2
    assert record["status"] == "verify_failed"
    assert entry.source == "pr_loop"
    assert entry.weight == 2.0


def test_load_overlay_empty(level_up_root: Path) -> None:
    overlay = load_overlay("agent-fleet", "coder")
    assert overlay.rules == ()
    assert overlay.generation == 0
    assert overlay.schema_version == 1


def test_load_overlay_reads_rules_and_generation(level_up_root: Path) -> None:
    directory = level_up_root / "agent-fleet" / "coder"
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(
        json.dumps({"generation": 3}),
        encoding="utf-8",
    )
    (directory / "overlay.yaml").write_text(
        """
schema_version: 1
rules:
  - id: verify-before-done
    kind: methodology
    text: Run verify before claiming done.
    confidence: 0.8
""".strip(),
        encoding="utf-8",
    )

    overlay = load_overlay("agent-fleet", "coder")
    assert overlay.generation == 3
    assert len(overlay.rules) == 1
    assert overlay.rules[0].id == "verify-before-done"
    assert overlay.rules[0].kind == "methodology"


def test_compose_overlay_prompt(level_up_root: Path) -> None:
    rules = (
        LevelUpRule(
            id="verify-before-done",
            kind="methodology",
            text="Run verify before claiming done.",
        ),
    )
    text = compose_overlay_prompt(rules, generation=2)
    assert text.startswith("# Level up (gen 2)")
    assert "verify-before-done" in text
    assert "Run verify before claiming done." in text


def test_compose_overlay_prompt_empty_rules() -> None:
    assert compose_overlay_prompt(()) == ""
