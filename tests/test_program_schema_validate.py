"""Pin the E1 schema validate+retry behavior of agent() in runtime.py."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import cast

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.program import run_workflow_program

_PROG = (
    'r = agent("go", schema={"type": "object", '
    '"properties": {"x": {"type": "number"}}, "required": ["x"]})\n'
    'return {"data": r.data, "err": r.schema_error}'
)


@dataclass
class _Dispatcher:
    summary: str = "ok"
    goals: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,  # noqa: ARG002
        same_workspace_tasks: int = 1,  # noqa: ARG002
        handoff: object = None,  # noqa: ARG002
        base_branch: str | None = None,  # noqa: ARG002
        depth: int = 1,  # noqa: ARG002
    ) -> FleetTaskResult:
        with self._lock:
            self.goals.append(task.goal)
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=self.summary,
            error=None,
            duration_seconds=0.0,
            observed_total_tokens=100,
        )


class _SeqDispatcher:
    """Pops summaries from a list in order; repeats the last when exhausted."""

    def __init__(self, summaries: list[str]) -> None:
        self._remaining = list(summaries)
        self._last = summaries[-1]
        self.goals: list[str] = []
        self._lock = threading.Lock()

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,  # noqa: ARG002
        same_workspace_tasks: int = 1,  # noqa: ARG002
        handoff: object = None,  # noqa: ARG002
        base_branch: str | None = None,  # noqa: ARG002
        depth: int = 1,  # noqa: ARG002
    ) -> FleetTaskResult:
        with self._lock:
            self.goals.append(task.goal)
            summary = self._remaining.pop(0) if self._remaining else self._last
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=summary,
            error=None,
            duration_seconds=0.0,
            observed_total_tokens=100,
        )


def test_valid_on_first_try_no_retry() -> None:
    disp = _Dispatcher(summary='{"x": 1}')
    summary = run_workflow_program(_PROG, dispatcher=disp)
    assert summary.status == "completed"
    assert summary.result == {"data": {"x": 1}, "err": None}
    assert summary.agents_dispatched == 1


def test_bad_then_good_retries_once() -> None:
    disp = _SeqDispatcher(["not json at all", '{"x": 1}'])
    summary = run_workflow_program(_PROG, dispatcher=disp)
    assert summary.status == "completed"
    assert summary.result == {"data": {"x": 1}, "err": None}
    assert summary.agents_dispatched == 2


def test_persistently_non_json_returns_none_data_and_error() -> None:
    disp = _SeqDispatcher(["nope", "nope"])
    summary = run_workflow_program(_PROG, dispatcher=disp)
    assert summary.status == "completed"
    assert isinstance(summary.result, dict)
    result = cast("dict[str, object]", summary.result)
    assert result["data"] is None
    assert "no JSON" in str(result["err"] or "")
    assert summary.agents_dispatched == 2


def test_json_violates_schema_preserves_best_attempt() -> None:
    disp = _Dispatcher(summary='{"y": 1}')
    summary = run_workflow_program(_PROG, dispatcher=disp)
    assert summary.status == "completed"
    assert isinstance(summary.result, dict)
    result = cast("dict[str, object]", summary.result)
    assert result["data"] == {"y": 1}
    assert "schema validation failed" in str(result["err"] or "")
    assert summary.agents_dispatched == 2


def test_no_schema_returns_none_data_no_retry() -> None:
    disp = _Dispatcher(summary="anything")
    prog = 'return agent("go").data'
    summary = run_workflow_program(prog, dispatcher=disp)
    assert summary.status == "completed"
    assert summary.result is None
    assert summary.agents_dispatched == 1
