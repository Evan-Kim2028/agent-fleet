"""End-to-end crash-resume through the operator CLI (Phase E real-product check).

Drives the actual ``cmd_dag_run`` and ``cmd_dag_resume`` handlers against a real
journal file on disk. Only the LLM backend and persona YAML are stubbed; the DAG
parse, journal write, fold, and re-dispatch decision are the product's own.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import agent_fleet.orchestration.dag.cli as cli
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.journal import load_journal, query_by_run

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


@dataclass
class _FakeDispatcher:
    fail: frozenset[str] = frozenset()
    calls: list[str] = field(default_factory=list)
    config: object = None

    def _execute_task(self, task_index: int, task: FleetTask, **_: object) -> FleetTaskResult:
        node_id = (task.title or "").rsplit(" — ", 1)[-1]
        self.calls.append(node_id)
        status = "error" if node_id in self.fail else "completed"
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status=status,
            summary=f"done {node_id}",
            error=None if status == "completed" else "boom",
            duration_seconds=0.0,
        )


class _FakeResolver:
    def list_personas(self) -> list[str]:
        return ["coder"]


_DIAMOND = {
    "title": "diamond",
    "tasks": [
        {"id": "a", "depends_on": [], "complexity": "LOW", "subtask_prompt": "a"},
        {"id": "b", "depends_on": ["a"], "complexity": "LOW", "subtask_prompt": "b"},
        {"id": "c", "depends_on": ["a"], "complexity": "LOW", "subtask_prompt": "c"},
        {"id": "d", "depends_on": ["b", "c"], "complexity": "LOW", "subtask_prompt": "d"},
    ],
}


def _ns(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "file": None,
        "workspace": None,
        "config": None,
        "canvas_path": None,
        "canvas": None,
        "canvases_dir": None,
        "canvas_debounce_ms": 200,
        "init_only": False,
        "dry_run": False,
        "json": True,
        "persona": None,
        "pipeline": None,
        "context": None,
        "journal": None,
        "run_id": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _patch_backend(monkeypatch: pytest.MonkeyPatch, dispatcher: _FakeDispatcher) -> None:
    monkeypatch.setattr(cli, "require_backend_env", lambda _cfg: None)
    monkeypatch.setattr(cli, "YamlPersonaResolver", lambda _cfg: _FakeResolver())
    monkeypatch.setattr(cli, "FleetDispatcher", lambda **_: dispatcher)


def test_cli_run_journals_then_resume_redispatches_only_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_file = tmp_path / "diamond.json"
    spec_file.write_text(json.dumps(_DIAMOND), encoding="utf-8")
    jpath = tmp_path / "run.jsonl"

    run_disp = _FakeDispatcher(fail=frozenset({"c"}))
    _patch_backend(monkeypatch, run_disp)
    run_code = cli.cmd_dag_run(
        _ns(file=str(spec_file), workspace=str(tmp_path), journal=str(jpath), run_id="r1")
    )

    assert run_code == 0  # dag_partial is a non-error exit
    assert sorted(run_disp.calls) == ["a", "b", "c"]
    assert jpath.exists()
    after_run = query_by_run(load_journal(jpath), "r1")
    assert after_run.completed_task_indices == frozenset({0, 1})

    resume_disp = _FakeDispatcher()
    _patch_backend(monkeypatch, resume_disp)
    resume_code = cli.cmd_dag_resume(
        _ns(file=str(spec_file), workspace=str(tmp_path), journal=str(jpath), run_id="r1")
    )

    assert resume_code == 0
    assert sorted(resume_disp.calls) == ["c", "d"]  # a, b reused from the journal
    after_resume = query_by_run(load_journal(jpath), "r1")
    assert after_resume.completed_task_indices == frozenset({0, 1, 2, 3})


def test_cli_resume_missing_journal_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_file = tmp_path / "diamond.json"
    spec_file.write_text(json.dumps(_DIAMOND), encoding="utf-8")
    _patch_backend(monkeypatch, _FakeDispatcher())

    code = cli.cmd_dag_resume(
        _ns(
            file=str(spec_file),
            workspace=str(tmp_path),
            journal=str(tmp_path / "nope.jsonl"),
            run_id="r1",
        )
    )
    assert code == 1
