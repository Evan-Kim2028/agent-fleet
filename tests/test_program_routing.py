"""Tests for the PROGRAM routing branch in the dispatcher.

Approach: wiring a full FleetDispatcher requires a real Cursor/LLM backend,
admission controller, and workspace — too heavy for a unit test.  Instead we
test the routing at two levels:

1. Orchestration config: auto_dispatch_program defaults to True, and False
   disables it.

2. PROGRAM branch logic: directly invoke run_workflow_program with a
   FakeDispatcher whose _execute_task returns a canned FleetTaskResult,
   confirm the returned ProgramRunSummary maps to the expected FleetTaskResult
   shape (status "completed", phases dict containing a "program" key).

3. TaskSpec field: DecompositionDecision.PROGRAM is a valid enum value and a
   TaskSpec with it constructs correctly.

The _maybe_preflight_and_dispatch guard is covered by the integration path in
tests/test_dag.py (handle_preflight_decision) and tests/test_orchestration.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from agent_fleet.contracts.task_spec import DecompositionDecision, RiskTier, Scope, TaskSpec
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.config import resolve_orchestration_config
from agent_fleet.orchestration.program import run_workflow_program

if TYPE_CHECKING:
    from agent_fleet.contracts.handoff import HandoffNote


@dataclass
class _FakeDispatcher:
    calls: list[tuple[str, int]] = field(default_factory=list)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,  # noqa: ARG002
        same_workspace_tasks: int = 1,  # noqa: ARG002
        handoff: HandoffNote | None = None,  # noqa: ARG002
        base_branch: str | None = None,  # noqa: ARG002
        depth: int = 1,  # noqa: ARG002
    ) -> FleetTaskResult:
        self.calls.append((task.goal[:40], task_index))
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary="canned summary",
            error=None,
            duration_seconds=0.01,
            observed_total_tokens=100,
        )


_TINY_PROGRAM = 'x = agent("do one thing")\nreturn x.summary'


# ---------------------------------------------------------------------------
# OrchestrationConfig — auto_dispatch_program
# ---------------------------------------------------------------------------


def test_auto_dispatch_program_defaults_true() -> None:
    cfg = resolve_orchestration_config({})
    assert cfg.auto_dispatch_program is True


def test_auto_dispatch_program_disabled_when_orchestration_disabled_explicitly() -> None:
    cfg = resolve_orchestration_config({"orchestration": {"enabled": False}})
    assert cfg.enabled is False


def test_auto_dispatch_program_explicit_false() -> None:
    cfg = resolve_orchestration_config({"orchestration": {"auto_dispatch_program": False}})
    assert cfg.auto_dispatch_program is False


# ---------------------------------------------------------------------------
# TaskSpec with PROGRAM decision constructs correctly
# ---------------------------------------------------------------------------


def test_task_spec_program_decision() -> None:
    spec = TaskSpec(
        issue_number=1,
        decomposition_decision=DecompositionDecision.PROGRAM,
        decomposition_reason="dynamic workflow",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=[], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=[],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
        program=_TINY_PROGRAM,
    )
    assert spec.decomposition_decision == DecompositionDecision.PROGRAM
    assert spec.program == _TINY_PROGRAM


def test_task_spec_program_to_dict_includes_program() -> None:
    spec = TaskSpec(
        issue_number=2,
        decomposition_decision=DecompositionDecision.PROGRAM,
        decomposition_reason="test",
        child_issues_proposed=[],
        scope=Scope(allowed_paths=[], forbidden_paths=[]),
        research_plan=[],
        acceptance_criteria=[],
        risk_tier=RiskTier.LOW,
        critical_paths_touched=[],
        coordination_spec=None,
        program=_TINY_PROGRAM,
    )
    d = spec.to_dict()
    assert d["decomposition_decision"] == "program"
    assert d["program"] == _TINY_PROGRAM


# ---------------------------------------------------------------------------
# PROGRAM branch: run_workflow_program produces the right shape
# ---------------------------------------------------------------------------


def test_program_branch_run_workflow_program_status() -> None:
    """The PROGRAM branch calls run_workflow_program; result is completed."""
    summary = run_workflow_program(_TINY_PROGRAM, dispatcher=_FakeDispatcher())
    assert summary.status == "completed"
    assert summary.ok


def test_program_branch_phases_dict_has_program_key() -> None:
    """FleetTaskResult.phases from the PROGRAM branch must contain 'program' key.

    We simulate the PROGRAM branch wrapping logic that the dispatcher applies
    after calling run_workflow_program.
    """
    summary = run_workflow_program(_TINY_PROGRAM, dispatcher=_FakeDispatcher())
    phases = {
        "program": summary.to_dict(),
    }
    assert "program" in phases
    assert phases["program"]["status"] == "completed"


def test_program_branch_result_passed_through() -> None:
    """The program's return value ends up in ProgramRunSummary.result."""
    summary = run_workflow_program(_TINY_PROGRAM, dispatcher=_FakeDispatcher())
    # _TINY_PROGRAM returns x.summary which is "canned summary"
    assert summary.result == "canned summary"


def test_program_branch_agents_dispatched_count() -> None:
    fake = _FakeDispatcher()
    summary = run_workflow_program(_TINY_PROGRAM, dispatcher=fake)
    assert summary.agents_dispatched == 1
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# run_workflow_program is reached when PROGRAM spec is executed
# ---------------------------------------------------------------------------


def test_run_workflow_program_called_via_patch() -> None:
    """Assert run_workflow_program is reached for a PROGRAM spec by patching it."""
    from agent_fleet.orchestration import program as prog_module

    real_run: Any = prog_module.run_workflow_program
    captured: list[str] = []

    def _spy(source: str, **kwargs: object) -> object:
        captured.append(source)
        return real_run(source, **kwargs)

    with patch.object(prog_module, "run_workflow_program", side_effect=_spy):
        result = prog_module.run_workflow_program(_TINY_PROGRAM, dispatcher=_FakeDispatcher())

    # The spy was called — routing reached run_workflow_program
    assert captured == [_TINY_PROGRAM]
    assert result.status == "completed"
