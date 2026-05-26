"""Experience recording from LocalFleetRunner and shared record helpers."""

# ruff: noqa: TC003

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_fleet.hooks import FleetTask
from agent_fleet.level_up import paths as level_up_paths
from agent_fleet.level_up.config import LevelUpConfig
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.level_up.record import (
    maybe_trigger_auto_learn,
    record_runner_experience,
    record_task_experience,
)
from agent_fleet.runner import FleetRunResult


@pytest.fixture
def level_up_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "level_up"
    monkeypatch.setattr(level_up_paths, "LEVEL_UP_ROOT", root)
    return root


def test_record_runner_experience_appends_row(
    level_up_root: Path,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(
        "name: runner-repo\nlevel_up:\n  train: true\n",
        encoding="utf-8",
    )
    equip = DispatchEquip(
        skill_slots_execute=("pstack/tdd",),
        skill_slots_review=(),
        level_up_generation=0,
        compose_body="Equipped body",
        base_loadout="coder",
        persona="coder",
    )
    result = FleetRunResult(
        run_id="run-abc",
        task_id=42,
        persona="coder",
        outcome="completed",
        changed_files=["src/foo.py"],
        reviews=[{"verdict": "approve"}],
    )
    record_runner_experience(
        result=result,
        title="Fix foo",
        persona="coder",
        repo_root=tmp_path,
        experience_source="issue_dispatch",
        dispatch_equip=equip,
    )

    exp_path = level_up_paths.persona_dir("runner-repo", "coder") / "experience.jsonl"
    assert exp_path.is_file()
    row = json.loads(exp_path.read_text(encoding="utf-8").strip())
    assert row["source"] == "issue_dispatch"
    assert row["status"] == "completed"
    assert row["review_verdict"] == "approve"
    assert row["goal"] == "Fix foo"
    assert row["equip_snapshot"]["base_loadout"] == "coder"


def test_record_task_experience_respects_train_false(
    level_up_root: Path,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    repo_yaml = tmp_path / ".agent-fleet.yaml"
    repo_yaml.write_text(
        "name: no-train\nlevel_up:\n  train: false\n",
        encoding="utf-8",
    )
    task = FleetTask(goal="x", persona="coder", workspace=str(tmp_path))
    record_task_experience(
        task=task,
        status="completed",
        workspace=tmp_path,
        run_id="r1",
    )
    exp_path = level_up_paths.persona_dir("no-train", "coder") / "experience.jsonl"
    assert not exp_path.exists()


def test_maybe_trigger_auto_learn_respects_cooldown(
    level_up_root: Path,  # noqa: ARG001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_fleet.repo import RepoConfig

    repo = RepoConfig(
        repo_root=tmp_path,
        name="learn-repo",
        level_up=LevelUpConfig(
            auto_learn=True,
            min_experience_rows=1,
            learn_cooldown_seconds=99999,
        ),
    )
    persona_dir = level_up_paths.persona_dir("learn-repo", "coder")
    persona_dir.mkdir(parents=True)
    (persona_dir / "experience.jsonl").write_text(
        json.dumps({"status": "completed", "source": "cli"}) + "\n",
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def _fake_trigger(
        *,
        personas: list[str] | None = None,
        dry_run: bool = False,
    ) -> object:
        del dry_run
        calls.append(list(personas or []))
        from agent_fleet.learning.synthesizer import FleetSynthesisResult

        return FleetSynthesisResult([], 0, 0, {})

    monkeypatch.setattr(
        "agent_fleet.learning.trigger_fleet_learning_cycle",
        _fake_trigger,
    )

    maybe_trigger_auto_learn(persona="coder", repo=repo)
    assert len(calls) == 1

    maybe_trigger_auto_learn(persona="coder", repo=repo)
    assert len(calls) == 1
