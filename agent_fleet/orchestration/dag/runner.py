"""Execute a DAG through the fleet dispatcher rank-by-rank."""

# ruff: noqa: TC001

from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.orchestration.convergence import compact_summary, is_failure, is_success
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
from agent_fleet.orchestration.primitives import DispatchPrimitives, effective_capacity

if TYPE_CHECKING:
    from agent_fleet.hooks import PersonaResolver
    from agent_fleet.observability.fleet_logger import FleetLogger
    from agent_fleet.orchestration.dag.canvas_writer import DagCanvasWriter
    from agent_fleet.orchestration.journal import RunJournal, RunState
    from agent_fleet.orchestration.types import _DispatcherLike
    from agent_fleet.persona_foundry import PersonaFoundry

logger = logging.getLogger(__name__)


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
    foundry: PersonaFoundry | None = None,
) -> FleetTask:
    known = set(persona_resolver.list_personas())
    persona = task.persona or parent_task.persona or fallback_persona
    if persona not in known:
        if foundry is not None:
            persona = foundry.resolve_or_generate(persona, known, fallback_persona)
        else:
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
        skills=task.skills,
    )


def aggregate_dag_results(results: list[FleetTaskResult]) -> tuple[str, str | None, str]:
    if not results:
        return "dag_failed", "No DAG tasks were dispatched", ""

    executed = [r for r in results if r.status != "skipped"]
    skipped = [r for r in results if r.status == "skipped"]
    summary = compact_summary(executed if executed else results)

    if executed and all(is_success(r.status) for r in executed):
        return "completed", None, summary
    if any(is_success(r.status) for r in executed):
        failed = [r for r in executed if is_failure(r.status)]
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
    dispatcher: _DispatcherLike,
    persona_resolver: PersonaResolver,
    fallback_persona: str,
    default_pipeline: str = "code_review",
    parent_run_id: str | None = None,
    max_chars_per_parent: int = 2000,
    acceptance_criteria: list[str] | None = None,
    fleet_log: FleetLogger | None = None,
    canvas_writer: DagCanvasWriter | None = None,
    foundry: PersonaFoundry | None = None,
    child_depth: int = 1,
    journal: RunJournal | None = None,
    resume_state: RunState | None = None,
) -> DagRunSummary:
    """Run DAG tasks rank-by-rank through the fleet dispatcher.

    ``child_depth`` is the admission-nesting level for the dispatched nodes
    (1 when run beneath a single token-holding parent). The AdmissionGate uses
    it to queue overflow instead of denying.

    ``journal`` (when given) records run/agent lifecycle events durably so the
    run can be resumed after a crash. ``resume_state`` (the folded journal of a
    prior run) makes the dispatch idempotent: tasks already finished are replayed
    from their recorded result instead of re-dispatched, so a resumed run
    converges to the same end state as one that never crashed.
    """
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

    if journal is not None and resume_state is None:
        journal.run_started()

    all_results: list[FleetTaskResult] = []
    upstream_outputs: dict[str, str] = {}
    failed_ids: set[str] = set()
    skip_ids: set[str] = set()

    # Dependency-driven dispatch: each task starts the moment its own
    # dependencies finish, not when its whole topological rank does, so
    # independent chains never wait on each other and wall-clock tracks the
    # critical path. All mutable state below is touched only on this thread in
    # the drain loop, so no lock is needed. same_workspace_tasks stays 1: DAG
    # nodes compose edits into the single parent workspace. The pool size is just
    # a worker hint; the AdmissionGate is the real bound and queues any overflow.
    by_id = {task.id: task for task in spec.tasks}
    remaining_deps = {task.id: len(task.depends_on) for task in spec.tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in spec.tasks}
    for task in spec.tasks:
        for dep in task.depends_on:
            dependents[dep].append(task.id)

    # Stable per-spec task identity for the journal: dispatch order is timing
    # dependent and not reproducible across runs, so resume keys on the task's
    # fixed position in spec.tasks, not on the volatile dispatch_index.
    stable_index = {task.id: i for i, task in enumerate(spec.tasks)}
    done_outputs: dict[str, str] = {}
    done_results: dict[str, FleetTaskResult] = {}
    if resume_state is not None:
        completed = resume_state.completed_task_indices
        for task in spec.tasks:
            i = stable_index[task.id]
            rec = resume_state.agent_by_index(i)
            if rec is None or i not in completed:
                continue
            done_outputs[task.id] = rec.output if rec.output is not None else (rec.summary or "")
            done_results[task.id] = FleetTaskResult(
                task_index=i,
                persona=rec.persona or task.persona or fallback_persona,
                goal=f"{spec.title} — {task.id}",
                status=rec.status,
                summary=rec.summary or "",
                error=rec.error,
                duration_seconds=0.0,
                observed_total_tokens=rec.observed_total_tokens,
            )
    done_ids = set(done_outputs)

    dispatch_index = 0
    n_total = len(spec.tasks)
    inflight = max(
        1,
        min(
            n_total,
            effective_capacity(dispatcher, fallback=n_total),
        ),
    )
    primitives = DispatchPrimitives(dispatcher, max_parallel=inflight)

    def _emit_skip(dag_task: DagTask) -> None:
        if canvas_state is not None:
            set_task_status(
                canvas_state,
                dag_task.id,
                status="ERROR",
                error_message="Skipped: upstream task failed",
                duration_ms=0,
            )
        all_results.append(
            FleetTaskResult(
                task_index=len(all_results),
                persona=dag_task.persona or fallback_persona,
                goal=f"{spec.title} — {dag_task.id}",
                status="skipped",
                summary="Skipped due to upstream failure",
                error=None,
                duration_seconds=0.0,
            )
        )

    def _replay_done(dag_task: DagTask) -> None:
        result = done_results[dag_task.id]
        all_results.append(result)
        upstream_outputs[dag_task.id] = done_outputs[dag_task.id]
        if canvas_state is not None:
            set_task_status(
                canvas_state,
                dag_task.id,
                status=fleet_status_to_canvas(result.status),
                result_text=done_outputs[dag_task.id] or None,
                duration_ms=0,
            )

    def _release(done_id: str) -> list[DagTask]:
        freed: list[DagTask] = []
        for child_id in dependents[done_id]:
            remaining_deps[child_id] -= 1
            if remaining_deps[child_id] == 0:
                freed.append(by_id[child_id])
        freed.sort(key=lambda task: task.id)
        return freed

    with ThreadPoolExecutor(max_workers=inflight) as pool:
        futures: dict[Future[FleetTaskResult], DagTask] = {}

        def _launch(ready: list[DagTask]) -> None:
            nonlocal dispatch_index
            for dag_task in ready:
                if dag_task.id in skip_ids:
                    _emit_skip(dag_task)
                    skip_ids.update(transitive_dependents(spec, {dag_task.id}))
                    _launch(_release(dag_task.id))
                    continue
                if dag_task.id in done_ids:
                    _replay_done(dag_task)
                    _launch(_release(dag_task.id))
                    continue
                fleet_task = fleet_task_from_dag_node(
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
                    foundry=foundry,
                )
                if journal is not None:
                    journal.agent_started(
                        stable_index[dag_task.id],
                        persona=fleet_task.persona,
                        goal=fleet_task.goal,
                    )
                if canvas_state is not None:
                    set_task_status(canvas_state, dag_task.id, status="RUNNING")
                futures[
                    pool.submit(
                        primitives.run_one,
                        dispatch_index,
                        fleet_task,
                        batch_size=n_total,
                        depth=child_depth,
                    )
                ] = dag_task
                dispatch_index += 1

        _launch(
            sorted(
                (task for task in spec.tasks if remaining_deps[task.id] == 0),
                key=lambda task: task.id,
            )
        )
        _sync_canvas()

        while futures:
            done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in done:
                dag_task = futures.pop(future)
                result = future.result()
                all_results.append(result)
                if journal is not None:
                    i = stable_index[dag_task.id]
                    if is_failure(result.status):
                        journal.agent_failed(i, error=result.error or result.status)
                    else:
                        journal.agent_completed(
                            i,
                            status=result.status,
                            summary=result.summary,
                            observed_total_tokens=result.observed_total_tokens,
                            output=_result_output(result),
                        )
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
                if is_success(result.status):
                    upstream_outputs[dag_task.id] = _result_output(result)
                elif is_failure(result.status):
                    failed_ids.add(dag_task.id)
                    skip_ids.update(transitive_dependents(spec, failed_ids))
                _launch(_release(dag_task.id))
            _sync_canvas()

    status, error, summary = aggregate_dag_results(all_results)
    if canvas_state is not None and canvas_writer is not None:
        outcome = "SUCCESS" if status == "completed" else "FAILED"
        finalize_run_state(canvas_state, outcome=outcome, message=error or summary)
        canvas_writer.schedule(canvas_state)
        canvas_writer.flush()

    if journal is not None:
        journal.run_completed(status=status)

    if fleet_log is not None:
        fleet_log.emit(
            "orchestration.dag.done",
            status=status,
            succeeded=sum(1 for r in all_results if is_success(r.status)),
            skipped=sum(1 for r in all_results if r.status == "skipped"),
        )

    return DagRunSummary(
        ranks=rank_ids,
        results=all_results,
        aggregate_status=status,
        error=error,
        summary=summary,
    )
