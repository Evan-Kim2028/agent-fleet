"""Execute a DAG through the fleet dispatcher rank-by-rank."""

# ruff: noqa: TC001

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.orchestration.dag.canvas_state import (
    finalize_run_state,
    fleet_status_to_canvas,
    initial_run_state,
    set_task_status,
)
from agent_fleet.orchestration.dag.scheduler import (
    topo_sort_ranks,
    transitive_dependents,
    validate_dag_graph,
)
from agent_fleet.orchestration.dag.schema import DagSpec, DagTask
from agent_fleet.orchestration.dag.stitch import build_dag_task_context

if TYPE_CHECKING:
    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.hooks import PersonaResolver
    from agent_fleet.observability.fleet_logger import FleetLogger
    from agent_fleet.orchestration.dag.canvas_writer import DagCanvasWriter

logger = logging.getLogger(__name__)

_SUCCESS_STATUSES = frozenset({"completed", "merged"})
_PARTIAL_OK = frozenset({"review_changes_requested"})
_FAILURE_STATUSES = frozenset(
    {"error", "verify_failed", "scope_violation", "review_blocked", "rejected"}
)


@dataclass(frozen=True)
class DagRunSummary:
    ranks: list[list[str]]
    results: list[FleetTaskResult]
    aggregate_status: str
    error: str | None
    summary: str


def _result_output(result: FleetTaskResult) -> str:
    parts: list[str] = []
    if result.summary:
        parts.append(result.summary.strip())
    phases: dict[str, object] = result.phases or {}
    execute = phases.get("execute")
    if isinstance(execute, dict):
        stdout = cast("dict[str, object]", execute).get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            parts.append(stdout.strip())
    if result.error:
        parts.append(f"Error: {result.error}")
    return "\n".join(parts).strip()


def _is_success(status: str) -> bool:
    return status in _SUCCESS_STATUSES | _PARTIAL_OK


def _is_failure(status: str) -> bool:
    terminal = _SUCCESS_STATUSES | _PARTIAL_OK | {"skipped"}
    return status in _FAILURE_STATUSES or status not in terminal


def fleet_task_from_dag_node(
    *,
    task: DagTask,
    spec: DagSpec,
    parent_task: FleetTask,
    upstream_outputs: dict[str, str],
    default_pipeline: str,
    fallback_persona: str,
    persona_resolver: PersonaResolver,
    parent_run_id: str | None,
    max_chars_per_parent: int,
    acceptance_criteria: list[str] | None = None,
) -> FleetTask:
    known = set(persona_resolver.list_personas())
    persona = task.persona or parent_task.persona or fallback_persona
    if persona not in known:
        logger.warning(
            "DAG node %r persona %r not in fleet; using %r",
            task.id,
            persona,
            fallback_persona,
        )
        persona = fallback_persona

    pipeline = task.pipeline or parent_task.pipeline or default_pipeline
    context = build_dag_task_context(
        task,
        dag_title=spec.title,
        parent_context=parent_task.context,
        upstream_outputs=upstream_outputs,
        acceptance_criteria=acceptance_criteria,
        max_chars_per_parent=max_chars_per_parent,
    )
    child_equip = (
        DispatchEquip(
            persona=persona,
            base_loadout=persona,
            skill_slots_execute=(),
            skill_slots_review=(),
            level_up_generation=0,
            parent_run_id=parent_run_id,
        )
        if parent_run_id
        else None
    )
    title = f"{spec.title} — {task.id}"
    return FleetTask(
        goal=title,
        context=context,
        persona=persona,
        workspace=parent_task.workspace,
        pipeline=pipeline,
        complexity=task.complexity,
        title=title,
        equip=child_equip,
        allowed_paths=task.allowed_paths,
    )


def aggregate_dag_results(results: list[FleetTaskResult]) -> tuple[str, str | None, str]:
    if not results:
        return "dag_failed", "No DAG tasks were dispatched", ""

    executed = [r for r in results if r.status != "skipped"]
    skipped = [r for r in results if r.status == "skipped"]
    successes = sum(1 for r in executed if _is_success(r.status))
    failures = sum(1 for r in executed if _is_failure(r.status))

    lines = [
        f"DAG run: {len(executed)} executed, {len(skipped)} skipped, "
        f"{successes} succeeded, {failures} failed."
    ]
    for result in results:
        label = result.goal[:80]
        lines.append(f"- {label} → {result.status}")
        if result.summary:
            lines.append(f"  {result.summary[:200]}")

    summary = "\n".join(lines)

    if failures == 0 and successes == len(executed) and executed:
        return "completed", None, summary
    if successes > 0:
        failed = [r for r in executed if _is_failure(r.status)]
        err = "; ".join(f"{r.goal[:40]}: {r.error or r.status}" for r in failed[:3])
        return "dag_partial", err or "Some DAG tasks failed", summary
    if skipped and not executed:
        return "dag_failed", "All DAG tasks were skipped", summary
    err = executed[0].error or executed[0].status if executed else "DAG failed"
    return "dag_failed", err, summary


def dispatch_dag(
    *,
    spec: DagSpec,
    parent_task: FleetTask,
    dispatcher: FleetDispatcher,
    persona_resolver: PersonaResolver,
    fallback_persona: str,
    default_pipeline: str = "code_review",
    parent_run_id: str | None = None,
    max_chars_per_parent: int = 2000,
    acceptance_criteria: list[str] | None = None,
    fleet_log: FleetLogger | None = None,
    canvas_writer: DagCanvasWriter | None = None,
) -> DagRunSummary:
    """Run DAG tasks rank-by-rank through the fleet dispatcher."""
    validate_dag_graph(spec)
    ranks = topo_sort_ranks(spec.tasks)
    rank_ids = [[task.id for task in rank] for rank in ranks]
    canvas_state = initial_run_state(spec) if canvas_writer is not None else None

    def _sync_canvas() -> None:
        if canvas_writer is not None and canvas_state is not None:
            canvas_writer.schedule(canvas_state)

    if canvas_writer is not None and canvas_state is not None:
        _sync_canvas()

    if fleet_log is not None:
        fleet_log.emit(
            "orchestration.dag.start",
            title=spec.title,
            task_count=len(spec.tasks),
            rank_count=len(ranks),
        )

    all_results: list[FleetTaskResult] = []
    upstream_outputs: dict[str, str] = {}
    failed_ids: set[str] = set()
    skip_ids: set[str] = set()

    for rank_index, rank in enumerate(ranks):
        runnable = [task for task in rank if task.id not in skip_ids]
        skipped_in_rank = [task for task in rank if task.id in skip_ids]
        for task in skipped_in_rank:
            if canvas_state is not None:
                set_task_status(
                    canvas_state,
                    task.id,
                    status="ERROR",
                    error_message="Skipped: upstream task failed",
                    duration_ms=0,
                )
            all_results.append(
                FleetTaskResult(
                    task_index=len(all_results),
                    persona=task.persona or fallback_persona,
                    goal=f"{spec.title} — {task.id}",
                    status="skipped",
                    summary="Skipped due to upstream failure",
                    error=None,
                    duration_seconds=0.0,
                )
            )
        if skipped_in_rank:
            _sync_canvas()

        if not runnable:
            continue

        if fleet_log is not None:
            fleet_log.emit(
                "orchestration.dag.rank_start",
                rank=rank_index,
                tasks=[task.id for task in runnable],
            )

        for dag_task in runnable:
            if canvas_state is not None:
                set_task_status(canvas_state, dag_task.id, status="RUNNING")
        _sync_canvas()

        fleet_tasks: list[tuple[DagTask, FleetTask]] = []
        for dag_task in runnable:
            fleet_tasks.append(
                (
                    dag_task,
                    fleet_task_from_dag_node(
                        task=dag_task,
                        spec=spec,
                        parent_task=parent_task,
                        upstream_outputs=upstream_outputs,
                        default_pipeline=default_pipeline,
                        fallback_persona=fallback_persona,
                        persona_resolver=persona_resolver,
                        parent_run_id=parent_run_id,
                        max_chars_per_parent=max_chars_per_parent,
                        acceptance_criteria=acceptance_criteria,
                    ),
                )
            )

        rank_results: list[FleetTaskResult | None] = [None] * len(fleet_tasks)
        if len(fleet_tasks) == 1:
            dag_task, fleet_task = fleet_tasks[0]
            result = dispatcher._execute_task(
                len(all_results),
                fleet_task,
                batch_size=1,
                same_workspace_tasks=1,
            )
            rank_results[0] = result
        else:
            with ThreadPoolExecutor(max_workers=len(fleet_tasks)) as pool:
                futures = {
                    pool.submit(
                        dispatcher._execute_task,
                        len(all_results) + idx,
                        fleet_task,
                        batch_size=len(fleet_tasks),
                        same_workspace_tasks=1,
                    ): (idx, dag_task)
                    for idx, (dag_task, fleet_task) in enumerate(fleet_tasks)
                }
                for future in as_completed(futures):
                    idx, dag_task = futures[future]
                    rank_results[idx] = future.result()

        for (dag_task, _), result in zip(fleet_tasks, rank_results, strict=True):
            assert result is not None
            all_results.append(result)
            if fleet_log is not None:
                fleet_log.emit(
                    "orchestration.dag.task_finish",
                    id=dag_task.id,
                    status=result.status,
                    duration_s=result.duration_seconds,
                )
            if canvas_state is not None:
                output = _result_output(result)
                canvas_status = fleet_status_to_canvas(result.status)
                set_task_status(
                    canvas_state,
                    dag_task.id,
                    status=canvas_status,
                    result_text=output if canvas_status == "FINISHED" else None,
                    error_message=(
                        (result.error or result.status) if canvas_status == "ERROR" else None
                    ),
                    duration_ms=int(result.duration_seconds * 1000),
                )
            if _is_success(result.status):
                upstream_outputs[dag_task.id] = _result_output(result)
            elif _is_failure(result.status):
                failed_ids.add(dag_task.id)

        _sync_canvas()

        if failed_ids:
            skip_ids |= transitive_dependents(spec, failed_ids)

    status, error, summary = aggregate_dag_results(all_results)
    if canvas_state is not None and canvas_writer is not None:
        outcome = "SUCCESS" if status == "completed" else "FAILED"
        finalize_run_state(canvas_state, outcome=outcome, message=error or summary)
        canvas_writer.schedule(canvas_state)
        canvas_writer.flush()

    if fleet_log is not None:
        fleet_log.emit(
            "orchestration.dag.done",
            status=status,
            succeeded=sum(1 for r in all_results if _is_success(r.status)),
            skipped=sum(1 for r in all_results if r.status == "skipped"),
        )

    return DagRunSummary(
        ranks=rank_ids,
        results=all_results,
        aggregate_status=status,
        error=error,
        summary=summary,
    )
