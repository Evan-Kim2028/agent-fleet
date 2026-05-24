"""Tests for phase_graph helpers used by LocalFleetRunner."""

from __future__ import annotations

from agent_fleet.contracts.task_spec import DecompositionDecision, RiskTier, Scope, TaskSpec
from agent_fleet.phase_graph import PhaseRunContext, default_phase_graph, should_run_phase


def _minimal_task_spec(*, risk: RiskTier = RiskTier.LOW) -> TaskSpec:
    return TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.SINGLE,
        decomposition_reason="ok",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=["src/"], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=["pass"],
        risk_tier=risk,
        critical_paths_touched=[],
        coordination_spec=None,
    )


def test_should_run_tech_lead_from_graph() -> None:
    graph = default_phase_graph()
    ctx = PhaseRunContext(task_spec=_minimal_task_spec(risk=RiskTier.HIGH))
    assert should_run_phase(graph, "TECH_LEAD", ctx) is True

    low_ctx = PhaseRunContext(task_spec=_minimal_task_spec(risk=RiskTier.LOW))
    assert should_run_phase(graph, "TECH_LEAD", low_ctx) is False


def test_design_review_only_when_enabled() -> None:
    disabled = default_phase_graph(design_review_enabled=False)
    enabled = default_phase_graph(design_review_enabled=True)
    ctx = PhaseRunContext(changed_files=["frontend/app.tsx"])

    assert should_run_phase(disabled, "DESIGN_REVIEW", ctx) is False
    assert should_run_phase(enabled, "DESIGN_REVIEW", ctx) is True
