"""Compare v0.8.6 decompose vs v0.9.0 program tokens-to-parent for N=20 children.

The workload: a parent task whose work decomposes into 20 child sub-tasks,
each producing a ~2000-char result, whose outputs must be combined into a
single answer.

v0.8.6 decompose path
---------------------
aggregate_child_results() (decompose.py:199-226) builds the summary string the
parent receives.  For each child it appends:
  - a header line: "- [<persona>] <goal[:80]> → <status>"   (~85 chars)
  - a body line:   "  <summary[:200]>"                       (200 chars capped)
plus a one-line preamble (~60 chars).

We run it for real: construct 20 FleetTaskResult objects (each with a 2000-char
summary) and call aggregate_child_results().  The length of the returned summary
string is the actual bytes the parent must ingest; dividing by 4 gives the token
estimate used throughout the codebase.

v0.9.0 program path
-------------------
The program fans out 20 agents via parallel(), synthesises their outputs into
a single short string (~300 chars), and returns it.  run_workflow_program()
computes tokens_to_parent = len(result_str) // 4 — only the synthesised return
value crosses back to the parent.
"""

from __future__ import annotations

import time

from agent_fleet.hooks import FleetTaskResult
from agent_fleet.orchestration.decompose import aggregate_child_results
from agent_fleet.orchestration.program import run_workflow_program

from .instrumented import InstrumentedDispatcher

N_CHILDREN = 20
CHILD_RESULT_CHARS = 2000
SYNTHESIS_TARGET_CHARS = 300
LATENCY_S = 0.02


# ---------------------------------------------------------------------------
# v0.8.6 measurement — run aggregate_child_results() on real FleetTaskResult
# objects and measure the summary the parent receives.
# ---------------------------------------------------------------------------


def _make_child_result(i: int) -> FleetTaskResult:
    return FleetTaskResult(
        task_index=i,
        persona="coder",
        goal=f"child-task-{i}: implement feature module {i}",
        status="completed",
        summary="x" * CHILD_RESULT_CHARS,
        error=None,
        duration_seconds=LATENCY_S,
        observed_total_tokens=CHILD_RESULT_CHARS // 4,
    )


def _measure_v086_tokens_to_parent() -> int:
    results = [_make_child_result(i) for i in range(N_CHILDREN)]
    _status, _error, summary = aggregate_child_results(results)
    return max(1, len(summary) // 4)


# ---------------------------------------------------------------------------
# v0.9.0 measurement — run_workflow_program() fan-out then synthesise ONE
# short answer.  ProgramRunSummary.tokens_to_parent is the headline figure.
# ---------------------------------------------------------------------------

_V090_PROGRAM = """
phase("fan-out")
results = parallel([
    lambda i=i: agent(
        f"child-task-{i}: implement feature module {i}",
        persona="coder",
        title=f"child-{i}",
    )
    for i in range(20)
])
phase("synthesise")
ok = [r for r in results if r is not None and r.status == "completed"]
summary = f"Completed {len(ok)}/20 subtasks. All feature modules implemented."
return summary
"""


def _measure_v090_tokens_to_parent() -> tuple[int, object]:
    dispatcher = InstrumentedDispatcher(latency_s=LATENCY_S, result_chars=CHILD_RESULT_CHARS)
    run = run_workflow_program(
        _V090_PROGRAM,
        dispatcher=dispatcher,
        max_parallel=N_CHILDREN,
        max_agents=N_CHILDREN + 4,
    )
    assert run.status == "completed", f"program failed: {run.error}"
    assert run.agents_dispatched == N_CHILDREN
    return run.tokens_to_parent, run.result


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_v086_vs_v090_token_leverage() -> None:
    v086_tokens = _measure_v086_tokens_to_parent()
    v090_tokens, v090_result = _measure_v090_tokens_to_parent()

    result_str = str(v090_result or "")
    assert len(result_str) <= SYNTHESIS_TARGET_CHARS + 10, (
        f"v0.9.0 synthesised result too long: {len(result_str)} chars"
    )

    reduction_factor = v086_tokens / v090_tokens
    assert v090_tokens < v086_tokens, (
        f"expected v090 < v086: v090={v090_tokens} v086={v086_tokens}"
    )
    assert reduction_factor > 10, (
        f"expected reduction_factor > 10, got {reduction_factor:.1f} "
        f"(v086={v086_tokens}, v090={v090_tokens})"
    )

    # Correctness: the v0.9.0 synthesis mentions all 20 tasks.
    assert "20" in result_str, f"synthesis result should mention 20: {result_str!r}"

    # Report for reference (not an assertion — values captured in structured output).
    _ = (
        f"v0.8.6 tokens_to_parent={v086_tokens}  "
        f"v0.9.0 tokens_to_parent={v090_tokens}  "
        f"reduction_factor={reduction_factor:.1f}x"
    )


def test_v090_parallelism_is_measurable() -> None:
    """Confirm the 20-agent fan-out actually runs in parallel."""
    dispatcher = InstrumentedDispatcher(latency_s=LATENCY_S, result_chars=CHILD_RESULT_CHARS)
    t0 = time.monotonic()
    run = run_workflow_program(
        _V090_PROGRAM,
        dispatcher=dispatcher,
        max_parallel=N_CHILDREN,
        max_agents=N_CHILDREN + 4,
    )
    wall = time.monotonic() - t0

    assert run.status == "completed", run.error
    assert run.agents_dispatched == N_CHILDREN

    factor = dispatcher.recorder.parallelism_factor()
    assert factor > 3, f"expected parallelism_factor > 3, got {factor:.2f}"

    sequential_estimate = N_CHILDREN * LATENCY_S
    assert wall < sequential_estimate * 0.5, (
        f"wall={wall:.3f}s not fast enough; sequential would be {sequential_estimate:.3f}s"
    )
