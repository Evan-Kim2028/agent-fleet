"""Tests for DAG task runner — schema, scheduler, stitch, and dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.task_spec import DecompositionDecision, RiskTier, Scope, TaskSpec
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.config import resolve_orchestration_config
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
    assert "1 executed" in summary


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
        node_id = task.title.rsplit(" — ", 1)[-1]
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
        dispatcher=dispatcher,  # type: ignore[arg-type]
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


def test_dag_validate_cli(capsys: pytest.CaptureFixture[str]) -> None:
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
    assert '"valid": true' in out
    assert "rank_count" in out
