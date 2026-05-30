"""Tests for the Unit 4 dynamic-control primitives: branch, replan, subprogram.

These exercise the runtime-dynamic surface: a running program judging a path,
looping under model control, and generating then running a bounded sub-program.
No real LLM is used; fake dispatchers return canned summaries (including the
JSON a judge would emit) so the control flow is deterministic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.program import (
    run_workflow_program,
    validate_workflow_program,
)
from agent_fleet.orchestration.program.runtime import (
    MAX_REPLAN_ITERATIONS,
    MAX_SUBPROGRAM_DEPTH,
)


@dataclass
class _Dispatcher:
    """Returns a fixed summary for every agent, recording goals thread-safely."""

    summary: str = "ok"
    goals: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,  # noqa: ARG002
        same_workspace_tasks: int = 1,  # noqa: ARG002
        handoff: object = None,  # noqa: ARG002
        base_branch: str | None = None,  # noqa: ARG002
        depth: int = 1,  # noqa: ARG002
    ) -> FleetTaskResult:
        with self._lock:
            self.goals.append(task.goal)
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=self.summary,
            error=None,
            duration_seconds=0.0,
            observed_total_tokens=100,
        )


# ---------------------------------------------------------------------------
# validator: a program built only out of dynamic primitives still dispatches
# ---------------------------------------------------------------------------


def test_validate_accepts_subprogram_only_program() -> None:
    result = validate_workflow_program('return subprogram("return agent(\\"x\\")")')
    assert result.ok
    assert result.uses_dynamic
    assert result.agent_calls == 0


def test_validate_accepts_branch_only_program() -> None:
    result = validate_workflow_program(
        'return branch("go?", lambda: agent("a"), lambda: agent("b"))'
    )
    assert result.ok
    assert result.uses_dynamic


def test_validate_still_rejects_truly_empty_program() -> None:
    result = validate_workflow_program("return 1 + 1")
    assert not result.ok


def test_dynamic_primitives_add_no_escape_surface() -> None:
    """The dynamic primitives are plain names; the escape bans are unchanged."""
    bad = validate_workflow_program('import os\nreturn subprogram("return agent(\\"x\\")")')
    assert not bad.ok
    assert any("Import" in e for e in bad.errors)


# ---------------------------------------------------------------------------
# branch: model-judged conditional
# ---------------------------------------------------------------------------

_BRANCH_PROG = (
    'picked = branch("ship it?", lambda: agent("ship"), lambda: agent("hold"))\nreturn picked.goal'
)


def test_branch_takes_true_path_on_yes() -> None:
    summary = run_workflow_program(_BRANCH_PROG, dispatcher=_Dispatcher(summary='{"answer": true}'))
    assert summary.status == "completed"
    assert summary.result == "ship"


def test_branch_takes_false_path_on_no() -> None:
    summary = run_workflow_program(
        _BRANCH_PROG, dispatcher=_Dispatcher(summary='{"answer": false}')
    )
    assert summary.status == "completed"
    assert summary.result == "hold"


def test_branch_returns_none_when_no_false_thunk() -> None:
    prog = 'out = branch("go?", lambda: agent("a"))\nreturn out'
    summary = run_workflow_program(prog, dispatcher=_Dispatcher(summary='{"answer": false}'))
    assert summary.status == "completed"
    assert summary.result is None


def test_branch_counts_judge_plus_chosen_agent() -> None:
    disp = _Dispatcher(summary='{"answer": true}')
    summary = run_workflow_program(_BRANCH_PROG, dispatcher=disp)
    assert summary.agents_dispatched == 2


# ---------------------------------------------------------------------------
# replan: model-triggered loop step
# ---------------------------------------------------------------------------

_REPLAN_PROG = (
    'hist = replan("converge", lambda i, last: agent("attempt " + str(i)))\nreturn len(hist)'
)


def test_replan_loops_to_cap_when_never_satisfied() -> None:
    summary = run_workflow_program(_REPLAN_PROG, dispatcher=_Dispatcher(summary='{"done": false}'))
    assert summary.status == "completed"
    assert summary.result == MAX_REPLAN_ITERATIONS


def test_replan_stops_early_when_satisfied() -> None:
    summary = run_workflow_program(_REPLAN_PROG, dispatcher=_Dispatcher(summary='{"done": true}'))
    assert summary.status == "completed"
    assert summary.result == 1


def test_replan_judge_not_run_after_final_round() -> None:
    """3 step agents + 2 judges (judge skipped after the last round) == 5."""
    disp = _Dispatcher(summary='{"done": false}')
    summary = run_workflow_program(_REPLAN_PROG, dispatcher=disp)
    assert summary.agents_dispatched == MAX_REPLAN_ITERATIONS + (MAX_REPLAN_ITERATIONS - 1)


def test_replan_caps_requested_iterations_at_max() -> None:
    prog = 'hist = replan("x", lambda i, last: agent("a"), max_iterations=99)\nreturn len(hist)'
    summary = run_workflow_program(prog, dispatcher=_Dispatcher(summary='{"done": false}'))
    assert summary.result == MAX_REPLAN_ITERATIONS


# ---------------------------------------------------------------------------
# subprogram: bounded runtime recursion (the arbitrary-Python capability)
# ---------------------------------------------------------------------------


def test_subprogram_runs_nested_and_returns_value() -> None:
    prog = 'inner = "x = agent(\\"leaf\\")\\nreturn x.summary"\nreturn subprogram(inner)'
    summary = run_workflow_program(prog, dispatcher=_Dispatcher(summary="leaf-done"))
    assert summary.status == "completed"
    assert summary.result == "leaf-done"


def test_subprogram_shares_parent_agent_budget() -> None:
    """The nested program's agent counts against the parent budget."""
    prog = 'inner = "x = agent(\\"leaf\\")\\nreturn x.summary"\nreturn subprogram(inner)'
    summary = run_workflow_program(prog, dispatcher=_Dispatcher(), max_agents=64)
    assert summary.agents_dispatched == 1


def test_subprogram_invalid_source_raises_error_status() -> None:
    prog = 'return subprogram("import os")'
    summary = run_workflow_program(prog, dispatcher=_Dispatcher())
    assert summary.status == "error"
    assert summary.error is not None
    assert "validation" in summary.error


def _nest_subprograms(times: int) -> str:
    src = 'x = agent("leaf")\nreturn x.summary'
    for _ in range(times):
        src = f"return subprogram({src!r})"
    return src


def test_subprogram_depth_within_cap_completes() -> None:
    summary = run_workflow_program(
        _nest_subprograms(MAX_SUBPROGRAM_DEPTH), dispatcher=_Dispatcher(summary="deep")
    )
    assert summary.status == "completed"
    assert summary.result == "deep"


def test_subprogram_depth_over_cap_raises() -> None:
    summary = run_workflow_program(
        _nest_subprograms(MAX_SUBPROGRAM_DEPTH + 1), dispatcher=_Dispatcher(summary="deep")
    )
    assert summary.status == "error"
    assert summary.error is not None
    assert "MAX_SUBPROGRAM_DEPTH" in summary.error


def test_subprogram_recursion_bounded_across_parallel_fanout() -> None:
    """A sub-program nested inside a parallel thunk inherits the chain depth.

    Without depth seeding the pool thread would reset to 0 and recursion could
    run unbounded; with it, the over-cap nest still trips the guard.
    """
    leaf = 'x = agent("leaf")\nreturn x.summary'
    nested = f"return subprogram({leaf!r})"
    # parent (d0) -> sub (d1) -> parallel thunk -> sub (d2) -> sub (d3) -> sub (d3>=3 raises)
    inside_parallel = f"return subprogram({nested!r})"
    par = f"r = parallel([lambda: subprogram({inside_parallel!r})])\nreturn r[0]"
    top = f"return subprogram({par!r})"
    summary = run_workflow_program(top, dispatcher=_Dispatcher(summary="deep"), max_agents=64)
    assert summary.status == "error"
    assert summary.error is not None
    assert "MAX_SUBPROGRAM_DEPTH" in summary.error
