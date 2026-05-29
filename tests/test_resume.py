"""Crash-resume (Unit 5): a DAG run journals its progress, and after a partial
failure resume_run re-dispatches only the unfinished tasks, reusing the rest, and
converges to the same end state as a run that never crashed.

The "crash" is modeled as a partial run: one task errors (so its dependents are
skipped), leaving the journal with some tasks finished and some not. No real LLM
is used; a fake dispatcher returns canned results keyed by task id.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.dag.runner import dispatch_dag
from agent_fleet.orchestration.dag.schema import DagSpec, DagTask
from agent_fleet.orchestration.journal import RunJournal, load_journal, query_by_run
from agent_fleet.orchestration.resume import resume_run

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.hooks import PersonaResolver


@dataclass
class _FakeDispatcher:
    """Completes every task unless its id is in ``fail``; records calls + contexts."""

    config: object = None
    fail: frozenset[str] = frozenset()
    calls: list[str] = field(default_factory=list)
    contexts: dict[str, str] = field(default_factory=dict)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        **_: object,
    ) -> FleetTaskResult:
        node_id = (task.title or "").rsplit(" — ", 1)[-1]
        self.calls.append(node_id)
        self.contexts[node_id] = task.context
        status = "error" if node_id in self.fail else "completed"
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status=status,
            summary=f"done {node_id}",
            error=None if status == "completed" else "boom",
            duration_seconds=0.0,
            observed_total_tokens=10,
        )


class _FakeResolver:
    def list_personas(self) -> list[str]:
        return ["coder"]


def _diamond() -> DagSpec:
    return DagSpec(
        title="diamond",
        tasks=(
            DagTask(id="a", depends_on=(), complexity="LOW", subtask_prompt="a"),
            DagTask(id="b", depends_on=("a",), complexity="LOW", subtask_prompt="b"),
            DagTask(id="c", depends_on=("a",), complexity="LOW", subtask_prompt="c"),
            DagTask(id="d", depends_on=("b", "c"), complexity="LOW", subtask_prompt="d"),
        ),
    )


def _parent() -> FleetTask:
    return FleetTask(
        goal="diamond", context="", persona="coder", workspace="/tmp/repo", pipeline="simple"
    )


def _run(spec: DagSpec, dispatcher: _FakeDispatcher, journal: RunJournal | None, resume_state=None):  # noqa: ANN001, ANN202
    return dispatch_dag(
        spec=spec,
        parent_task=_parent(),
        dispatcher=cast("FleetDispatcher", dispatcher),
        persona_resolver=cast("PersonaResolver", _FakeResolver()),
        fallback_persona="coder",
        default_pipeline="simple",
        journal=journal,
        resume_state=resume_state,
    )


# ---------------------------------------------------------------------------
# Partial run leaves a journal that fold reads as partially complete
# ---------------------------------------------------------------------------


def test_partial_run_journals_completed_and_failed(tmp_path: Path) -> None:
    spec = _diamond()
    disp = _FakeDispatcher(fail=frozenset({"c"}))
    jpath = tmp_path / "run.jsonl"
    with RunJournal(jpath, "run-1") as journal:
        summary = _run(spec, disp, journal)

    assert summary.aggregate_status == "dag_partial"
    assert disp.calls == ["a", "b", "c"] or sorted(disp.calls) == ["a", "b", "c"]
    assert "d" not in disp.calls  # downstream of failed c was skipped

    state = query_by_run(load_journal(jpath), "run-1")
    # a=0, b=1 completed; c=2 failed; d=3 skipped (never journaled).
    assert state.completed_task_indices == frozenset({0, 1})


# ---------------------------------------------------------------------------
# Resume re-dispatches ONLY the unfinished tasks and converges to completed
# ---------------------------------------------------------------------------


def test_resume_redispatches_only_pending(tmp_path: Path) -> None:
    spec = _diamond()
    jpath = tmp_path / "run.jsonl"

    with RunJournal(jpath, "run-1") as journal:
        _run(spec, _FakeDispatcher(fail=frozenset({"c"})), journal)

    resume_disp = _FakeDispatcher()  # fails nothing this time
    summary = resume_run(
        journal_path=jpath,
        run_id="run-1",
        spec=spec,
        parent_task=_parent(),
        dispatcher=cast("FleetDispatcher", resume_disp),
        persona_resolver=cast("PersonaResolver", _FakeResolver()),
        fallback_persona="coder",
        default_pipeline="simple",
    )

    assert sorted(resume_disp.calls) == ["c", "d"]  # a, b reused, not re-run
    assert summary.aggregate_status == "completed"
    statuses = {r.goal.rsplit(" — ", 1)[-1]: r.status for r in summary.results}
    assert statuses == {"a": "completed", "b": "completed", "c": "completed", "d": "completed"}


def test_resume_reused_outputs_flow_to_downstream(tmp_path: Path) -> None:
    """The reused upstream output (b, recorded in the first run) reaches d."""
    spec = _diamond()
    jpath = tmp_path / "run.jsonl"
    with RunJournal(jpath, "run-1") as journal:
        _run(spec, _FakeDispatcher(fail=frozenset({"c"})), journal)

    resume_disp = _FakeDispatcher()
    resume_run(
        journal_path=jpath,
        run_id="run-1",
        spec=spec,
        parent_task=_parent(),
        dispatcher=cast("FleetDispatcher", resume_disp),
        persona_resolver=cast("PersonaResolver", _FakeResolver()),
        fallback_persona="coder",
        default_pipeline="simple",
    )
    d_context = resume_disp.contexts["d"]
    assert "done b" in d_context  # reused from the crashed run's journal
    assert "done c" in d_context  # freshly produced on resume


def test_resume_of_completed_run_dispatches_nothing(tmp_path: Path) -> None:
    """Idempotent convergence: resuming an already-finished run is a no-op."""
    spec = _diamond()
    jpath = tmp_path / "run.jsonl"

    with RunJournal(jpath, "run-1") as journal:
        _run(spec, _FakeDispatcher(fail=frozenset({"c"})), journal)
    resume_run(
        journal_path=jpath,
        run_id="run-1",
        spec=spec,
        parent_task=_parent(),
        dispatcher=cast("FleetDispatcher", _FakeDispatcher()),
        persona_resolver=cast("PersonaResolver", _FakeResolver()),
        fallback_persona="coder",
        default_pipeline="simple",
    )

    final_disp = _FakeDispatcher()
    summary = resume_run(
        journal_path=jpath,
        run_id="run-1",
        spec=spec,
        parent_task=_parent(),
        dispatcher=cast("FleetDispatcher", final_disp),
        persona_resolver=cast("PersonaResolver", _FakeResolver()),
        fallback_persona="coder",
        default_pipeline="simple",
    )
    assert final_disp.calls == []
    assert summary.aggregate_status == "completed"


def test_resume_seq_continues_so_completed_beats_stale_failed(tmp_path: Path) -> None:
    """The retried task's resume completion must out-rank its crash-run failure.

    Without seq continuation the resumed agent_completed would carry a lower seq
    than the crash-run agent_failed and lose last-writer-wins, leaving the task
    failed forever. This asserts the fold sees it completed.
    """
    spec = _diamond()
    jpath = tmp_path / "run.jsonl"
    with RunJournal(jpath, "run-1") as journal:
        _run(spec, _FakeDispatcher(fail=frozenset({"c"})), journal)
    resume_run(
        journal_path=jpath,
        run_id="run-1",
        spec=spec,
        parent_task=_parent(),
        dispatcher=cast("FleetDispatcher", _FakeDispatcher()),
        persona_resolver=cast("PersonaResolver", _FakeResolver()),
        fallback_persona="coder",
        default_pipeline="simple",
    )

    state = query_by_run(load_journal(jpath), "run-1")
    assert state.completed_task_indices == frozenset({0, 1, 2, 3})
    c = state.agent_by_index(2)
    assert c is not None
    assert c.status == "completed"
