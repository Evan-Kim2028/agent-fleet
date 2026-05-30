# ruff: noqa: TC002
"""Tests for Unit 1: per-task skill selection threaded end-to-end into dispatch equip.

Contract under test:
  DagTask.skills (tuple[str,...]) threads via fleet_task_from_dag_node into
  FleetTask.skills; resolve_dispatch_equip(task, fleet_config, repo=None,
  run_id=None) merges FleetTask.skills into equip.skill_slots_execute and into
  equip.compose_body (the composed prompt body).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import FleetTask
from agent_fleet.orchestration.dag.runner import fleet_task_from_dag_node
from agent_fleet.orchestration.dag.schema import dag_spec_from_dict
from agent_fleet.orchestration.equip import resolve_dispatch_equip

if TYPE_CHECKING:
    from agent_fleet.hooks import PersonaResolver

ROOT = Path(__file__).resolve().parent.parent

_SKILL_ID = "agent-skills/performance-optimization"
# A stable phrase from the performance-optimization SKILL.md body.
_SKILL_SUBSTRING = "Measure before optimizing"


def _patch_level_up_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr("agent_fleet.level_up.paths.LEVEL_UP_ROOT", root)


def test_task_skill_lands_in_execute_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A skill in FleetTask.skills appears in equip.skill_slots_execute."""
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="t", persona="coder", skills=(_SKILL_ID,))

    equip = resolve_dispatch_equip(task, fleet_config, repo=None, run_id=None)

    assert _SKILL_ID in equip.skill_slots_execute


def test_task_skill_content_in_compose_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SKILL.md content for a task skill appears in equip.compose_body."""
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="t", persona="coder", skills=(_SKILL_ID,))

    equip = resolve_dispatch_equip(task, fleet_config, repo=None, run_id=None)

    assert _SKILL_SUBSTRING in equip.compose_body


def test_nonexistent_skill_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A skill not present in base-kit is silently dropped from skill_slots_execute."""
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")

    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    task = FleetTask(goal="t", persona="coder", skills=("agent-skills/does-not-exist",))

    equip = resolve_dispatch_equip(task, fleet_config, repo=None, run_id=None)

    assert "agent-skills/does-not-exist" not in equip.skill_slots_execute


class _Resolver:
    def list_personas(self) -> list[str]:
        return ["coder"]


def test_dag_node_threads_skills_into_dispatched_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DagTask.skills survives fleet_task_from_dag_node and reaches the execute prompt."""
    _patch_level_up_root(monkeypatch, tmp_path / "level_up")
    spec = dag_spec_from_dict(
        {
            "title": "demo",
            "tasks": [
                {
                    "id": "opt",
                    "depends_on": [],
                    "complexity": "LOW",
                    "subtask_prompt": "speed it up",
                    "skills": [_SKILL_ID],
                }
            ],
        }
    )
    parent = FleetTask(goal="demo", persona="coder", workspace="/tmp/repo", pipeline="code_review")

    dispatched = fleet_task_from_dag_node(
        task=spec.tasks[0],
        spec=spec,
        parent_task=parent,
        upstream_outputs={},
        default_pipeline="code_review",
        fallback_persona="coder",
        persona_resolver=cast("PersonaResolver", _Resolver()),
        parent_run_id=None,
        max_chars_per_parent=2000,
    )

    assert dispatched.skills == (_SKILL_ID,)

    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    equip = resolve_dispatch_equip(dispatched, fleet_config, repo=None, run_id=None)
    assert _SKILL_ID in equip.skill_slots_execute
    assert _SKILL_SUBSTRING in equip.compose_body


def test_dag_task_skills_roundtrip() -> None:
    """DagTask.skills parses from dict and serialises back correctly."""
    spec_dict: dict[str, object] = {
        "title": "skill roundtrip",
        "tasks": [
            {
                "id": "task-a",
                "depends_on": [],
                "complexity": "LOW",
                "subtask_prompt": "do the thing",
                "skills": [_SKILL_ID],
            }
        ],
    }

    spec = dag_spec_from_dict(spec_dict)  # type: ignore[arg-type]

    assert spec.tasks[0].skills == (_SKILL_ID,)

    roundtripped = spec.to_dict()
    assert roundtripped["tasks"][0]["skills"] == [_SKILL_ID]
