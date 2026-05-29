"""Idempotent crash-resume for a DAG run, driven by its durable journal (D3).

A run writes a ``RunJournal`` as it dispatches. If the process dies mid-run, the
journal holds every agent that reached a terminal state, fsync-durable per event.
``resume_run`` folds that journal back into a ``RunState``, hands the finished-task
set to ``dispatch_dag``, and re-dispatches only the work that did not complete.
Re-running converges to the same end state, so a resumed run is indistinguishable
from one that never crashed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.orchestration.dag.runner import dispatch_dag
from agent_fleet.orchestration.journal import RunJournal, load_journal, query_by_run

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.hooks import FleetTask, PersonaResolver
    from agent_fleet.observability.fleet_logger import FleetLogger
    from agent_fleet.orchestration.dag.canvas_writer import DagCanvasWriter
    from agent_fleet.orchestration.dag.runner import DagRunSummary
    from agent_fleet.orchestration.dag.schema import DagSpec
    from agent_fleet.persona_foundry import PersonaFoundry


def resume_run(
    *,
    journal_path: str | Path,
    run_id: str,
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
    foundry: PersonaFoundry | None = None,
    child_depth: int = 1,
) -> DagRunSummary:
    """Resume a crashed DAG run, re-dispatching only the tasks that did not finish.

    Reads the journal at ``journal_path``, folds the events for ``run_id`` into a
    ``RunState``, and continues the run against ``spec``. Finished tasks are
    replayed from the journal; the rest are dispatched. The same journal file is
    reopened with its seq counter continued past the events already on disk, so a
    second crash mid-resume can itself be resumed.
    """
    events = load_journal(journal_path)
    state = query_by_run(events, run_id)
    next_seq = 1 + max((e.seq for e in events if e.run_id == run_id), default=-1)

    with RunJournal(journal_path, run_id, start_seq=next_seq) as journal:
        return dispatch_dag(
            spec=spec,
            parent_task=parent_task,
            dispatcher=dispatcher,
            persona_resolver=persona_resolver,
            fallback_persona=fallback_persona,
            default_pipeline=default_pipeline,
            parent_run_id=parent_run_id,
            max_chars_per_parent=max_chars_per_parent,
            acceptance_criteria=acceptance_criteria,
            fleet_log=fleet_log,
            canvas_writer=canvas_writer,
            foundry=foundry,
            child_depth=child_depth,
            journal=journal,
            resume_state=state,
        )
