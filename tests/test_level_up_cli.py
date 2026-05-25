"""Tests for level-up CLI commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agent_fleet.cli import cmd_level_up_journal, cmd_level_up_status, cmd_level_up_train
from agent_fleet.level_up import paths as level_up_paths


@pytest.fixture
def level_up_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "level_up"
    monkeypatch.setattr(level_up_paths, "LEVEL_UP_ROOT", root)
    return root


def _write_repo_config(repo_root: Path, *, name: str = "demo-repo") -> Path:
    config_path = repo_root / ".agent-fleet.yaml"
    config_path.write_text(
        f"name: {name}\ndefault_persona: coder\n",
        encoding="utf-8",
    )
    return config_path


def test_level_up_status_reports_generation_and_rules(
    tmp_path: Path,
    level_up_root: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config(repo_root)

    persona_dir = level_up_root / "demo-repo" / "coder"
    persona_dir.mkdir(parents=True)
    (persona_dir / "meta.json").write_text('{"generation": 3}', encoding="utf-8")
    (persona_dir / "overlay.yaml").write_text(
        "rules:\n  - id: verify-before-done\n    text: run tests\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(repo=str(repo_root), persona="coder")
    assert cmd_level_up_status(args) == 0


def test_level_up_status_output(capsys, tmp_path: Path, level_up_root: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config(repo_root)

    persona_dir = level_up_root / "demo-repo" / "coder"
    persona_dir.mkdir(parents=True)
    (persona_dir / "meta.json").write_text('{"generation": 2}', encoding="utf-8")
    (persona_dir / "overlay.yaml").write_text("rules: []\n", encoding="utf-8")

    args = argparse.Namespace(repo=str(repo_root), persona="coder")
    cmd_level_up_status(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["generation"] == 2
    assert payload["rule_count"] == 0
    assert payload["repo_key"] == "demo-repo"


def test_level_up_journal_tail(tmp_path: Path, level_up_root: Path, capsys) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config(repo_root)

    persona_dir = level_up_root / "demo-repo" / "coder"
    persona_dir.mkdir(parents=True)
    journal = persona_dir / "journal.jsonl"
    journal.write_text(
        "\n".join(
            [
                '{"event":"equip.loadout","generation":1}',
                '{"event":"run.complete","outcome":"completed"}',
                '{"event":"experience.appended","source":"dispatch"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(repo=str(repo_root), persona="coder", tail=2)
    assert cmd_level_up_journal(args) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 2
    assert entries[0]["event"] == "run.complete"
    assert entries[1]["event"] == "experience.appended"


def test_load_repo_config_parses_level_up(tmp_path: Path) -> None:
    from agent_fleet.repo import load_repo_config

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config = repo_root / ".agent-fleet.yaml"
    config.write_text(
        "name: demo\nlevel_up:\n  train: false\n  contribute_to_fleet: false\n",
        encoding="utf-8",
    )

    repo = load_repo_config(config)
    assert repo.level_up is not None
    assert repo.level_up.train is False
    assert repo.level_up.contribute_to_fleet is False
    assert repo.level_up.journal_task_summaries is True


def test_level_up_train_skips_when_train_disabled(
    tmp_path: Path,
    level_up_root: Path,
    capsys,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo_config(repo_root, name="demo-repo")
    config_path = repo_root / ".agent-fleet.yaml"
    config_path.write_text(
        "name: demo-repo\nlevel_up:\n  train: false\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(repo=str(repo_root), persona="coder", dry_run=False)
    assert cmd_level_up_train(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] is True
