"""Tests for orchestration — auto-decompose and child dispatch."""

from __future__ import annotations

from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.task_spec import (
    DecompositionDecision,
    RiskTier,
    Scope,
    TaskSpec,
)
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.config import resolve_orchestration_config
from agent_fleet.orchestration.decompose import (
    aggregate_child_results,
    child_tasks_from_task_spec,
    coerce_empty_decompose,
    enrich_task_from_task_spec,
    handle_preflight_decision,
)
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import load_repo_config

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _task_spec(*, decision: DecompositionDecision, children: list[dict] | None = None) -> TaskSpec:
    return TaskSpec(
        issue_number=42,
        decomposition_decision=decision,
        decomposition_reason="cross-cutting change",
        child_issues_proposed=list(children or []),
        scope=Scope(allowed_paths=["api/", "web/"], forbidden_paths=[".github/"]),
        research_plan=[],
        acceptance_criteria=["Tests pass", "Scope respected"],
        risk_tier=RiskTier.MEDIUM,
        critical_paths_touched=[],
        coordination_spec={
            "interface_brief": {
                "kind": "http_route",
                "route": "/api/settings",
                "notes": "Shared contract for siblings",
            }
        },
    )


def test_orchestration_config_defaults() -> None:
    cfg = resolve_orchestration_config({})
    assert cfg.enabled is True
    assert cfg.auto_dispatch_children is True
    assert cfg.preflight_on_code_review is False
    assert cfg.default_child_pipeline == "code_review"


def test_orchestration_config_disabled() -> None:
    cfg = resolve_orchestration_config({"orchestration": False})
    assert cfg.enabled is False
    assert cfg.auto_dispatch_children is False


def test_orchestration_config_from_repo_example() -> None:
    repo = load_repo_config(ROOT / "examples" / "repo.agent-fleet.yaml")
    assert repo.orchestration is not None
    assert repo.orchestration.enabled is True
    assert repo.orchestration.preflight_on_code_review is True


def test_child_tasks_from_task_spec() -> None:
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    spec = _task_spec(
        decision=DecompositionDecision.DECOMPOSE,
        children=[
            {
                "title": "Backend API",
                "body": "Add settings endpoint",
                "persona": "coder",
                "allowed_paths": ["api/"],
            },
            {
                "title": "Frontend toggle",
                "body": "Add settings UI",
                "persona": "coder",
                "allowed_paths": ["web/"],
            },
        ],
    )
    parent = FleetTask(
        goal="Ship dark mode",
        context="Umbrella goal",
        persona="coder",
        workspace="/tmp/repo",
        pipeline="code_review",
    )
    children = child_tasks_from_task_spec(
        spec,
        parent_task=parent,
        child_pipeline="code_review",
        persona_resolver=resolver,
        fallback_persona="coder",
    )
    assert len(children) == 2
    assert children[0].pipeline == "code_review"
    assert (
        "interface_brief" in children[0].context.lower()
        or "Interface contract" in children[0].context
    )
    assert "api/" in children[0].context
    assert "Parent acceptance criteria" in children[1].context


def test_enrich_task_from_task_spec() -> None:
    spec = _task_spec(decision=DecompositionDecision.SINGLE)
    task = FleetTask(goal="Fix bug", context="see auth.py", persona="coder")
    enriched = enrich_task_from_task_spec(task, spec)
    assert "Acceptance criteria" in enriched.context
    assert "Planned scope" in enriched.context


def test_aggregate_child_results_all_success() -> None:
    results = [
        FleetTaskResult(
            task_index=0,
            persona="coder",
            goal="A",
            status="completed",
            summary="done",
            error=None,
            duration_seconds=1.0,
        ),
        FleetTaskResult(
            task_index=1,
            persona="coder",
            goal="B",
            status="merged",
            summary="merged",
            error=None,
            duration_seconds=2.0,
        ),
    ]
    status, error, summary = aggregate_child_results(results)
    assert status == "completed"
    assert error is None
    assert "2 child task" in summary


def test_aggregate_child_results_partial() -> None:
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
            status="scope_violation",
            summary=None,
            error="out of scope",
            duration_seconds=2.0,
        ),
    ]
    status, error, _summary = aggregate_child_results(results)
    assert status == "decompose_partial"
    assert error is not None


def test_coerce_empty_decompose_falls_back_to_single() -> None:
    spec = _task_spec(decision=DecompositionDecision.DECOMPOSE, children=[])
    coerced, fell_back = coerce_empty_decompose(spec)
    assert fell_back is True
    assert coerced.decomposition_decision == DecompositionDecision.SINGLE
    assert "orchestration fallback" in coerced.decomposition_reason


def test_coerce_empty_decompose_keeps_decompose_with_children() -> None:
    spec = _task_spec(
        decision=DecompositionDecision.DECOMPOSE,
        children=[{"title": "A", "body": "b", "persona": "coder"}],
    )
    coerced, fell_back = coerce_empty_decompose(spec)
    assert fell_back is False
    assert coerced.decomposition_decision == DecompositionDecision.DECOMPOSE


def test_handle_preflight_decision() -> None:
    rejected = _task_spec(decision=DecompositionDecision.REJECTED)
    status, err = handle_preflight_decision(rejected)
    assert status == "rejected"
    assert err == "cross-cutting change"

    decompose = _task_spec(
        decision=DecompositionDecision.DECOMPOSE,
        children=[{"title": "Split A", "body": "Do backend slice", "persona": "coder"}],
    )
    status, err = handle_preflight_decision(decompose)
    assert status == "decompose"

    empty_decompose = _task_spec(decision=DecompositionDecision.DECOMPOSE, children=[])
    status, err = handle_preflight_decision(empty_decompose)
    assert status == "single"
    assert err is None

    single = _task_spec(decision=DecompositionDecision.SINGLE)
    status, err = handle_preflight_decision(single)
    assert status == "single"
    assert err is None
