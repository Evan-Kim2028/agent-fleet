"""Tests for the workflow-program runtime, validator, and token accounting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.program import (
    run_workflow_program,
    validate_workflow_program,
)


@dataclass
class _FakeDispatcher:
    calls: list[tuple[int, str, int, int]] = field(default_factory=list)

    def _execute_task(
        self,
        idx: int,
        task: FleetTask,
        *,
        batch_size: int = 1,
        same_workspace_tasks: int = 1,
        handoff: object = None,  # noqa: ARG002
        base_branch: object = None,  # noqa: ARG002
    ) -> FleetTaskResult:
        self.calls.append((idx, task.goal[:40], batch_size, same_workspace_tasks))
        time.sleep(0.01)
        return FleetTaskResult(
            task_index=idx,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=f"done idx={idx}",
            error=None,
            duration_seconds=0.01,
            observed_total_tokens=5000,
        )


# ---------------------------------------------------------------------------
# validate_workflow_program
# ---------------------------------------------------------------------------

def test_validate_rejects_import() -> None:
    result = validate_workflow_program("import os\nreturn agent('x')")
    assert not result.ok
    assert any("Import" in e for e in result.errors)


def test_validate_rejects_dunder_attr() -> None:
    result = validate_workflow_program("x = ().__class__\nreturn agent('a')")
    assert not result.ok
    assert any("dunder" in e or "_" in e for e in result.errors)


def test_validate_rejects_zero_agents() -> None:
    result = validate_workflow_program("return 1 + 1")
    assert not result.ok
    assert any("agent" in e.lower() for e in result.errors)


def test_validate_accepts_valid_single_agent() -> None:
    result = validate_workflow_program("x = agent('do something')\nreturn x")
    assert result.ok
    assert result.agent_calls == 1


def test_validate_counts_agent_calls() -> None:
    src = "a = agent('a')\nb = agent('b')\nreturn a"
    result = validate_workflow_program(src)
    assert result.ok
    assert result.agent_calls == 2


# ---------------------------------------------------------------------------
# run_workflow_program — basic correctness
# ---------------------------------------------------------------------------

_COMPLEX_PROG = """\
phase("scan")
findings = parallel([
    lambda: agent("audit module A"),
    lambda: agent("audit module B"),
    lambda: agent("audit module C"),
])
ok = [f for f in findings if f and f.ok]
log(str(len(ok)) + " ok")
graded = pipeline(
    ["x", "y"],
    lambda prev, orig, i: agent("grade " + orig),
)
solo = agent("final synthesis")
result = {"audited": len(findings), "ok": len(ok), "graded": len([g for g in graded if g])}
result["solo_ok"] = solo.ok
return result
"""


def test_run_workflow_completed_status() -> None:
    summary = run_workflow_program(_COMPLEX_PROG, dispatcher=_FakeDispatcher())
    assert summary.status == "completed"
    assert summary.ok


def test_run_workflow_six_agents_dispatched() -> None:
    summary = run_workflow_program(_COMPLEX_PROG, dispatcher=_FakeDispatcher())
    assert summary.agents_dispatched == 6


def test_run_workflow_converged_result() -> None:
    summary = run_workflow_program(_COMPLEX_PROG, dispatcher=_FakeDispatcher())
    assert summary.result == {"audited": 3, "ok": 3, "graded": 2, "solo_ok": True}


def test_run_workflow_phases_recorded() -> None:
    summary = run_workflow_program(_COMPLEX_PROG, dispatcher=_FakeDispatcher())
    assert summary.phases == ("scan",)


# ---------------------------------------------------------------------------
# ISOLATION: same_workspace_tasks per dispatch kind
# ---------------------------------------------------------------------------

def test_parallel_agents_isolated_sws2() -> None:
    fake = _FakeDispatcher()
    run_workflow_program(_COMPLEX_PROG, dispatcher=fake)
    parallel_batches = [c[3] for c in fake.calls if c[1].startswith("audit")]
    assert len(parallel_batches) == 3
    assert all(b == 2 for b in parallel_batches)


def test_pipeline_agents_isolated_sws2() -> None:
    fake = _FakeDispatcher()
    run_workflow_program(_COMPLEX_PROG, dispatcher=fake)
    pipeline_batches = [c[3] for c in fake.calls if c[1].startswith("grade")]
    assert len(pipeline_batches) == 2
    assert all(b == 2 for b in pipeline_batches)


def test_solo_agent_sws1() -> None:
    fake = _FakeDispatcher()
    run_workflow_program(_COMPLEX_PROG, dispatcher=fake)
    solo_batches = [c[3] for c in fake.calls if c[1].startswith("final")]
    assert solo_batches == [1]


# ---------------------------------------------------------------------------
# HARD STOP: budget enforced — regression guard
# ---------------------------------------------------------------------------

_BUDGET_PROG = "\n".join(f"agent('task{i}')" for i in range(5)) + "\nreturn 1"


def test_budget_enforced_returns_error_status() -> None:
    summary = run_workflow_program(_BUDGET_PROG, dispatcher=_FakeDispatcher(), max_agents=3)
    assert summary.status == "error"


def test_budget_error_contains_budget_in_message() -> None:
    summary = run_workflow_program(_BUDGET_PROG, dispatcher=_FakeDispatcher(), max_agents=3)
    assert summary.error is not None
    assert "budget" in summary.error


def test_budget_parallel_thunk_not_silently_none() -> None:
    """Agent inside parallel thunk that hits budget must propagate error, not return list of None."""  # noqa: E501
    prog = "\n".join(
        [
            "results = parallel([",
            *[f"    lambda: agent('p{i}')," for i in range(5)],
            "])",
            "return results",
        ]
    )
    summary = run_workflow_program(prog, dispatcher=_FakeDispatcher(), max_agents=2)
    assert summary.status == "error"
    assert summary.error is not None
    assert "budget" in summary.error


# ---------------------------------------------------------------------------
# TOKEN ACCOUNTING
# ---------------------------------------------------------------------------

def test_tokens_across_agents_sum() -> None:
    summary = run_workflow_program(_COMPLEX_PROG, dispatcher=_FakeDispatcher())
    assert summary.tokens_across_agents == 6 * 5000


def test_tokens_to_parent_small() -> None:
    summary = run_workflow_program(_COMPLEX_PROG, dispatcher=_FakeDispatcher())
    assert summary.tokens_to_parent < 100


def test_context_leverage_high() -> None:
    summary = run_workflow_program(_COMPLEX_PROG, dispatcher=_FakeDispatcher())
    assert summary.context_leverage > 100


# ---------------------------------------------------------------------------
# to_dict round-trip
# ---------------------------------------------------------------------------

def test_program_run_summary_to_dict_keys() -> None:
    summary = run_workflow_program("x = agent('hi')\nreturn x.summary", dispatcher=_FakeDispatcher())  # noqa: E501
    d = summary.to_dict()
    assert set(d.keys()) >= {
        "status", "result", "error", "agents_dispatched", "agents_ok",
        "phases", "log", "tokens_across_agents", "tokens_to_parent",
        "context_leverage", "duration_seconds",
    }
