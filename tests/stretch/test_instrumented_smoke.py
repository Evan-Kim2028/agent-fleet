"""Smoke tests for InstrumentedDispatcher via run_workflow_program."""

from __future__ import annotations

from agent_fleet.orchestration.program import run_workflow_program

from .instrumented import InstrumentedDispatcher

_PROGRAM = """
results = parallel([
    lambda: agent("task A", persona="coder"),
    lambda: agent("task B", persona="coder"),
    lambda: agent("task C", persona="coder"),
])
return [r.status for r in results]
"""


def test_smoke_parallel_three_agents() -> None:
    dispatcher = InstrumentedDispatcher(latency_s=0.02, result_chars=2000)
    summary = run_workflow_program(
        _PROGRAM,
        dispatcher=dispatcher,
        max_parallel=8,
        max_agents=8,
    )

    assert summary.status == "completed", summary.error
    assert summary.agents_dispatched == 3
    assert summary.agents_ok == 3

    factor = dispatcher.recorder.parallelism_factor()
    assert factor > 1.5, f"expected parallelism_factor > 1.5, got {factor:.2f}"
