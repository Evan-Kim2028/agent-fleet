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
