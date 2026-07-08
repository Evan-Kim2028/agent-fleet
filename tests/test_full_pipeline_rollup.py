"""Full-pipeline runner carries its per-phase token rollup out on the result.

The dispatcher reads result.usage_rollup after LocalFleetRunner's bind_run
scope has closed, so RESEARCH/PLAN/SYNTHESIZE usage would otherwise be lost.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.observability.context import bind_run
from agent_fleet.observability.log import RunLog
from agent_fleet.runner import FleetRunResult, _run_end_kwargs

if TYPE_CHECKING:
    from pathlib import Path


def test_run_end_kwargs_stamps_research_usage_on_result(tmp_path: Path) -> None:
    run_log = RunLog.create(
        run_id="rollup-test",
        issue_number=1,
        task_id=0,
        persona="coder",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    result = FleetRunResult(run_id="rollup-test", task_id=0, persona="coder", outcome="completed")

    with bind_run(run_log, run_log.context):
        run_log.llm_usage(
            phase="RESEARCH",
            model="composer-2.5",
            duration_s=1.0,
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=300,
        )
        _run_end_kwargs(result, None)

    assert result.usage_rollup is not None
    by_phase = result.usage_rollup["by_phase"]
    assert "RESEARCH" in by_phase, "research-phase usage dropped from rollup"
    assert by_phase["RESEARCH"]["total_tokens"] == 1500


def test_run_end_kwargs_rollup_none_without_usage(tmp_path: Path) -> None:
    run_log = RunLog.create(
        run_id="rollup-empty",
        task_id=0,
        persona="coder",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    result = FleetRunResult(run_id="rollup-empty", task_id=0, persona="coder", outcome="completed")

    with bind_run(run_log, run_log.context):
        _run_end_kwargs(result, None)

    assert result.usage_rollup is None


def test_run_end_success_path_does_not_duplicate_pr_number(tmp_path: Path) -> None:
    """Happy path after OPEN_PR: pr_number comes only from _run_end_kwargs."""
    from agent_fleet.observability.sinks import MemoryRingSink

    ring = MemoryRingSink(max_events=20)
    run_log = RunLog.create(
        run_id="pr-dup-test",
        task_id=2454,
        persona="coder",
        runs_dir=tmp_path,
        include_memory_ring=False,
    )
    run_log._sinks.append(ring)
    terminal = FleetRunResult(
        run_id="pr-dup-test",
        task_id=2454,
        persona="coder",
        outcome="completed",
        pr_number=2460,
    )

    with bind_run(run_log, run_log.context):
        run_log.run_end(
            outcome=terminal.outcome,
            changed_lines=42,
            jsonl=str(run_log.jsonl_path) if run_log.jsonl_path else None,
            **_run_end_kwargs(terminal, None),
        )

    end_events = [e for e in ring.events if e.event == "run.end"]
    assert len(end_events) == 1
    payload = end_events[0].data
    assert payload is not None
    assert payload["pr_number"] == 2460
