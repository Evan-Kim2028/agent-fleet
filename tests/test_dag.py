# ruff: noqa: TC003
"""Tests for DAG task runner — schema, scheduler, stitch, and dispatch."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from agent_fleet.dispatcher import FleetDispatcher

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.task_spec import DecompositionDecision, RiskTier, Scope, TaskSpec
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.config import resolve_orchestration_config
from agent_fleet.orchestration.dag.ascii import render_dag_ascii
from agent_fleet.orchestration.dag.canvas_state import initial_run_state
from agent_fleet.orchestration.dag.runner import aggregate_dag_results, dispatch_dag
from agent_fleet.orchestration.dag.scheduler import topo_sort_ranks, validate_dag_graph
from agent_fleet.orchestration.dag.schema import DagSpec, DagTask, load_dag_spec
from agent_fleet.orchestration.dag.stitch import build_upstream_context, truncate
from agent_fleet.orchestration.decompose import handle_preflight_decision

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _example_spec() -> DagSpec:
    return DagSpec(
        title="OAuth integration",
        tasks=(
            DagTask(
                id="research-a",
                depends_on=(),
                complexity="LOW",
                subtask_prompt="Research API patterns",
            ),
            DagTask(
                id="research-b",
                depends_on=(),
                complexity="LOW",
                subtask_prompt="Research UI patterns",
            ),
            DagTask(
                id="implement-api",
                depends_on=("research-a",),
                complexity="MED",
                subtask_prompt="Implement API",
                allowed_paths=("api/",),
            ),
            DagTask(
                id="implement-ui",
                depends_on=("research-b", "implement-api"),
                complexity="MED",
                subtask_prompt="Implement UI",
                allowed_paths=("web/",),
            ),
        ),
    )


def test_topo_sort_ranks_diamond() -> None:
    ranks = topo_sort_ranks(_example_spec().tasks)
    assert [task.id for task in ranks[0]] == ["research-a", "research-b"]
    assert [task.id for task in ranks[1]] == ["implement-api"]
    assert [task.id for task in ranks[2]] == ["implement-ui"]


def test_validate_dag_graph_rejects_unknown_dependency() -> None:
    spec = DagSpec(
        title="bad",
        tasks=(
            DagTask(
                id="a",
                depends_on=("missing",),
                complexity="LOW",
                subtask_prompt="x",
            ),
        ),
    )
    with pytest.raises(ValueError, match="unknown id"):
        validate_dag_graph(spec)


def test_validate_dag_graph_rejects_cycle() -> None:
    spec = DagSpec(
        title="cycle",
        tasks=(
            DagTask(id="a", depends_on=("b",), complexity="LOW", subtask_prompt="a"),
            DagTask(id="b", depends_on=("a",), complexity="LOW", subtask_prompt="b"),
        ),
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_dag_graph(spec)


def test_load_example_dag_json() -> None:
    spec = load_dag_spec(ROOT / "examples" / "dag" / "example_dag.json")
    validate_dag_graph(spec)
    ranks = topo_sort_ranks(spec.tasks)
    assert len(ranks) == 3


def test_build_upstream_context_truncates() -> None:
    long_text = "x" * 3000
    ctx = build_upstream_context(
        DagTask(id="child", depends_on=("parent",), complexity="LOW", subtask_prompt="go"),
        {"parent": long_text},
        max_chars_per_parent=100,
    )
    assert "parent" in ctx
    assert len(ctx) < 3000


def test_truncate_ellipsis() -> None:
    assert truncate("hello world", 8) == "hello..."


def test_aggregate_dag_results_completed() -> None:
    results = [
        FleetTaskResult(
            task_index=0,
            persona="coder",
            goal="A",
            status="completed",
            summary="ok",
            error=None,
            duration_seconds=1.0,
        )
    ]
    status, error, summary = aggregate_dag_results(results)
    assert status == "completed"
    assert error is None
    assert "1/1" in summary


def test_build_upstream_context_multi_dep_budget() -> None:
    long_text = "x" * 2000
    task = DagTask(
        id="child",
        depends_on=("a", "b", "c", "d"),
        complexity="LOW",
        subtask_prompt="go",
    )
    outputs = dict.fromkeys(task.depends_on, long_text)
    ctx = build_upstream_context(task, outputs, max_chars_per_parent=2000)
    assert len(ctx) <= 2200


def test_build_upstream_context_single_dep_keeps_budget() -> None:
    long_text = "y" * 2000
    task = DagTask(id="child", depends_on=("only",), complexity="LOW", subtask_prompt="go")
    ctx = build_upstream_context(task, {"only": long_text}, max_chars_per_parent=2000)
    assert len(ctx) >= 1900


def test_aggregate_dag_results_partial() -> None:
    results = [
        FleetTaskResult(
            task_index=0,
            persona="coder",
            goal="A",
            status="completed",
            summary=None,
            error=None,
            duration_seconds=1.0,
        ),
        FleetTaskResult(
            task_index=1,
            persona="coder",
            goal="B",
            status="error",
            summary=None,
            error="boom",
            duration_seconds=1.0,
        ),
    ]
    status, error, _summary = aggregate_dag_results(results)
    assert status == "dag_partial"
    assert error is not None


def test_handle_preflight_decision_dag() -> None:
    spec = TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.DAG,
        decomposition_reason="ordered dependencies",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=[], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=[],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
        dag={"title": "t", "tasks": []},
    )
    status, err = handle_preflight_decision(spec)
    assert status == "dag"
    assert err is not None


def test_orchestration_config_dag_defaults() -> None:
    cfg = resolve_orchestration_config({})
    assert cfg.auto_dispatch_dag is True
    assert cfg.default_dag_pipeline == "code_review"
    assert cfg.dag_upstream_context_chars == 2000


@dataclass
class _FakeDispatcher:
    config: object
    calls: list[str] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)

    def _execute_task(self, task_index: int, task: FleetTask, **_: object) -> FleetTaskResult:
        node_id = (task.title or "").rsplit(" — ", 1)[-1]
        self.calls.append(node_id)
        status = self.outcomes.get(node_id, "completed")
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status=status,
            summary=f"done {node_id}",
            error=None if status == "completed" else "failed",
            duration_seconds=0.1,
        )


def test_dispatch_dag_skips_downstream_on_failure() -> None:
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    from agent_fleet.personas import YamlPersonaResolver

    resolver = YamlPersonaResolver(fleet_config)
    dispatcher = _FakeDispatcher(config=fleet_config, outcomes={"implement-api": "error"})
    parent = FleetTask(goal="parent", persona="coder", workspace="/tmp/repo", pipeline="simple")

    summary = dispatch_dag(
        spec=_example_spec(),
        parent_task=parent,
        dispatcher=cast("FleetDispatcher", dispatcher),
        persona_resolver=resolver,
        fallback_persona="coder",
        default_pipeline="simple",
    )

    assert "research-a" in dispatcher.calls
    assert "research-b" in dispatcher.calls
    assert "implement-api" in dispatcher.calls
    assert "implement-ui" not in dispatcher.calls
    skipped = [r for r in summary.results if r.status == "skipped"]
    assert len(skipped) == 1
    assert summary.aggregate_status == "dag_partial"


def test_dag_subcommand_registered() -> None:
    from agent_fleet.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["dag", "--help"])
    assert exc.value.code == 0


def test_dag_validate_cli_ascii(capsys: pytest.CaptureFixture[str]) -> None:
    from agent_fleet.cli import main

    code = main(
        [
            "dag",
            "validate",
            "--file",
            str(ROOT / "examples" / "dag" / "example_dag.json"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Rank 1" in out
    assert "research-api" in out
    assert '"valid"' not in out


def test_dag_validate_cli_json(capsys: pytest.CaptureFixture[str]) -> None:
    from agent_fleet.cli import main

    code = main(
        [
            "dag",
            "validate",
            "--file",
            str(ROOT / "examples" / "dag" / "example_dag.json"),
            "--json",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"valid": true' in out
    assert "rank_count" in out


def test_render_dag_ascii() -> None:
    spec = _example_spec()
    ranks = topo_sort_ranks(spec.tasks)
    text = render_dag_ascii(spec, ranks)
    assert "OAuth integration" in text
    assert "research-a" in text
    assert "implement-ui" in text


def test_canvas_writer_writes_tsx(tmp_path: Path) -> None:
    from agent_fleet.orchestration.dag.canvas_writer import DagCanvasWriter

    spec = _example_spec()
    path = tmp_path / "run.canvas.tsx"
    writer = DagCanvasWriter(path, debounce_ms=0)
    writer.schedule(initial_run_state(spec))
    writer.flush()
    source = path.read_text(encoding="utf-8")
    assert "cursor/canvas" in source
    assert "research-a" in source
    assert "PENDING" in source


def test_resolve_canvas_path() -> None:
    from agent_fleet.orchestration.dag.paths import resolve_canvas_path

    path = resolve_canvas_path(
        workspace=ROOT,
        canvas="my-dag",
    )
    assert path.name == "my-dag.canvas.tsx"
    assert path.suffix == ".tsx"


def _dag_parser() -> argparse.ArgumentParser:
    from agent_fleet.orchestration.dag.cli import register_dag_commands

    parser = argparse.ArgumentParser(prog="agent-fleet")
    sub = parser.add_subparsers(dest="command", required=True)
    register_dag_commands(sub)
    return parser


def test_dag_run_workspace_after_subcommand_resolves() -> None:
    """--workspace after `run` binds to the run namespace (the correct form)."""
    args = _dag_parser().parse_args(["dag", "run", "--file", "x.json", "--workspace", "/tmp/repo"])
    assert args.workspace == "/tmp/repo"


def test_dag_run_workspace_before_subcommand_is_rejected() -> None:
    """--workspace before `run` must fail loudly, not silently fall back to cwd.

    Regression: it was defined on both the `dag` parent and the `run` subparser,
    so the subparser's None default clobbered the parent value and the run
    silently targeted cwd (wrong repo → coder fallback → out-of-lane wander).
    """
    with pytest.raises(SystemExit):
        _dag_parser().parse_args(["dag", "--workspace", "/tmp/repo", "run", "--file", "x.json"])
