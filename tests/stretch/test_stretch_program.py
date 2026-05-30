"""Stretch tests for run_workflow_program via InstrumentedDispatcher.

Each test asserts a real correctness property AND records timing/token metrics.
No real LLM or composer is called — all dispatch is in-process and deterministic.
"""

from __future__ import annotations

from typing import cast

from agent_fleet.orchestration.program import run_workflow_program

from .instrumented import InstrumentedDispatcher

# ---------------------------------------------------------------------------
# Scenario 1 — wide_fanout
# 50 parallel agents, each returns ~2000 chars; parent summary is ~200 chars.
# ---------------------------------------------------------------------------

_WIDE_FANOUT_PROGRAM = """
results = parallel([
    lambda: agent(f"fanout task {i}", persona="coder", title=f"fanout-{i}")
    for i in range(50)
])
ok = [r for r in results if r is not None and r.status == "completed"]
summary_parts = [r.summary[:4] for r in ok[:10]]
return "SUMMARY:" + "|".join(summary_parts)
"""


def test_wide_fanout() -> None:
    dispatcher = InstrumentedDispatcher(latency_s=0.02, result_chars=2000)
    summary = run_workflow_program(
        _WIDE_FANOUT_PROGRAM,
        dispatcher=dispatcher,
        max_parallel=8,
        max_agents=64,
    )

    assert summary.status == "completed", summary.error
    assert summary.agents_dispatched == 50
    assert len(summary.agent_results) == 50

    factor = dispatcher.recorder.parallelism_factor()
    assert factor > 3, f"expected parallelism_factor > 3, got {factor:.2f}"

    result_str = str(summary.result or "")
    assert len(result_str) < 400, f"summary too large: {len(result_str)} chars"
    assert summary.tokens_to_parent < 100, (
        f"tokens_to_parent={summary.tokens_to_parent} should be small"
    )

    leverage = summary.context_leverage
    assert leverage > 10, f"expected leverage > 10, got {leverage:.1f}"


# ---------------------------------------------------------------------------
# Scenario 2 — deep_pipeline
# pipeline() of 5 stages over 10 items; assert per-item ordering preserved
# and all 10 final results present.
# ---------------------------------------------------------------------------

_DEEP_PIPELINE_PROGRAM = """
items = list(range(10))

def stage1(prev, orig, idx):
    r = agent(f"stage1 item {orig}", persona="coder", title=f"s1-{orig}")
    return {"idx": idx, "s1": r.status}

def stage2(prev, orig, idx):
    r = agent(f"stage2 item {orig}", persona="coder", title=f"s2-{orig}")
    return {**prev, "s2": r.status}

def stage3(prev, orig, idx):
    r = agent(f"stage3 item {orig}", persona="coder", title=f"s3-{orig}")
    return {**prev, "s3": r.status}

def stage4(prev, orig, idx):
    r = agent(f"stage4 item {orig}", persona="coder", title=f"s4-{orig}")
    return {**prev, "s4": r.status}

def stage5(prev, orig, idx):
    r = agent(f"stage5 item {orig}", persona="coder", title=f"s5-{orig}")
    return {**prev, "s5": r.status, "orig": orig}

results = pipeline(items, stage1, stage2, stage3, stage4, stage5)
return results
"""


def test_deep_pipeline() -> None:
    dispatcher = InstrumentedDispatcher(latency_s=0.01, result_chars=500)
    summary = run_workflow_program(
        _DEEP_PIPELINE_PROGRAM,
        dispatcher=dispatcher,
        max_parallel=8,
        max_agents=64,
    )

    assert summary.status == "completed", summary.error
    assert summary.agents_dispatched == 50, f"expected 50 calls, got {summary.agents_dispatched}"

    results = summary.result
    assert isinstance(results, list), f"result is not list: {type(results)}"
    assert len(results) == 10, f"expected 10 pipeline results, got {len(results)}"

    for i, item in enumerate(results):
        assert isinstance(item, dict), f"item {i} is not dict: {item!r}"
        item_d = cast("dict[str, object]", item)
        for stage in ("s1", "s2", "s3", "s4", "s5"):
            assert stage in item_d, f"item {i} missing stage {stage}: {item_d}"
            assert item_d[stage] == "completed", f"item {i} stage {stage} not completed"
        assert item_d["orig"] == i, f"item {i} has wrong orig={item_d['orig']}"


# ---------------------------------------------------------------------------
# Scenario 3 — branch_converge
# Fan out; branch on a Python condition; converge to ONE result.
# tokens_to_parent stays bounded regardless of branch count.
# ---------------------------------------------------------------------------

_BRANCH_CONVERGE_PROGRAM = """
items = list(range(12))

def classify(val, _orig, _idx):
    r = agent(f"classify {val}", persona="coder", title=f"cls-{val}")
    return {"val": val, "status": r.status, "bucket": "even" if val % 2 == 0 else "odd"}

classified = pipeline(items, classify)

evens = [c for c in classified if c and c.get("bucket") == "even"]
odds  = [c for c in classified if c and c.get("bucket") == "odd"]

even_results = parallel([
    lambda v=e["val"]: agent(f"process even {v}", persona="coder", title=f"pe-{v}")
    for e in evens
])
odd_results = parallel([
    lambda v=o["val"]: agent(f"process odd {v}", persona="coder", title=f"po-{v}")
    for o in odds
])

n_even_ok = sum(1 for r in even_results if r and r.status == "completed")
n_odd_ok  = sum(1 for r in odd_results  if r and r.status == "completed")

converge_r = agent("converge all branches", persona="coder", title="converge")
return f"even={n_even_ok} odd={n_odd_ok} converge={converge_r.status}"
"""


def test_branch_converge() -> None:
    dispatcher = InstrumentedDispatcher(latency_s=0.01, result_chars=2000)
    summary = run_workflow_program(
        _BRANCH_CONVERGE_PROGRAM,
        dispatcher=dispatcher,
        max_parallel=8,
        max_agents=64,
    )

    assert summary.status == "completed", summary.error

    result_str = str(summary.result or "")
    assert "even=6" in result_str, f"expected even=6 in result: {result_str!r}"
    assert "odd=6" in result_str, f"expected odd=6 in result: {result_str!r}"
    assert "converge=completed" in result_str, f"converge missing: {result_str!r}"

    assert summary.tokens_to_parent < 50, (
        f"tokens_to_parent={summary.tokens_to_parent} should be bounded"
    )
    assert summary.agents_dispatched == 25, (
        f"expected 25 agents (12 classify + 6 even + 6 odd + 1 converge), "
        f"got {summary.agents_dispatched}"
    )


# ---------------------------------------------------------------------------
# Scenario 4 — fanout_with_failure
# parallel() with a few error-status agents; program still converges,
# agents_ok < agents_dispatched.
# ---------------------------------------------------------------------------

_FANOUT_FAILURE_PROGRAM = """
results = parallel([
    lambda i=i: agent(f"worker {i}", persona="coder", title=f"worker-{i}")
    for i in range(20)
])
ok_count   = sum(1 for r in results if r is not None and r.status == "completed")
err_count  = sum(1 for r in results if r is not None and r.status == "error")
none_count = sum(1 for r in results if r is None)
return {"ok": ok_count, "err": err_count, "none": none_count, "total": len(results)}
"""


def test_fanout_with_failure() -> None:
    fail_labels = {f"worker-{i}" for i in range(0, 20, 4)}
    dispatcher = InstrumentedDispatcher(
        latency_s=0.01,
        result_chars=500,
        fail_labels=fail_labels,
    )
    summary = run_workflow_program(
        _FANOUT_FAILURE_PROGRAM,
        dispatcher=dispatcher,
        max_parallel=8,
        max_agents=64,
    )

    assert summary.status == "completed", summary.error
    assert summary.agents_dispatched == 20

    result = summary.result
    assert isinstance(result, dict), f"result is not dict: {result!r}"
    result_d = cast("dict[str, object]", result)
    assert result_d["total"] == 20
    assert (
        cast("int", result_d["ok"]) + cast("int", result_d["err"]) + cast("int", result_d["none"])
        == 20
    )

    assert summary.agents_ok < summary.agents_dispatched, (
        f"expected some failures: ok={summary.agents_ok}, dispatched={summary.agents_dispatched}"
    )
    failed_count = summary.agents_dispatched - summary.agents_ok
    assert failed_count >= len(fail_labels), (
        f"expected at least {len(fail_labels)} failures, got {failed_count}"
    )
