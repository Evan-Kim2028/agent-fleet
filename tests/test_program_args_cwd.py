"""Tests pinning E3 args/cwd parameterization of run_workflow_program."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.program import run_workflow_program


@dataclass
class _Dispatcher:
    """Returns a fixed summary for every agent, recording goals thread-safely."""

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


# Programs must include at least one agent() call to pass validation.
# The agent result is ignored; args/cwd are what's under test.

def test_args_and_cwd_passthrough() -> None:
    source = 'agent("noop")\nreturn [args.get("repo"), cwd]'
    summary = run_workflow_program(
        source,
        dispatcher=_Dispatcher(),
        args={"repo": "acme"},
        cwd="/tmp/work",
    )
    assert summary.result == ["acme", "/tmp/work"]


def test_args_and_cwd_defaults() -> None:
    source = 'agent("noop")\nreturn [args, cwd]'
    summary = run_workflow_program(source, dispatcher=_Dispatcher())
    assert summary.result == [{}, None]


def test_args_multiple_keys_sorted() -> None:
    source = 'agent("noop")\nreturn sorted(args.keys())'
    summary = run_workflow_program(
        source,
        dispatcher=_Dispatcher(),
        args={"b": 2, "a": 1},
    )
    assert summary.result == ["a", "b"]


def test_args_is_mutable_plain_dict() -> None:
    source = 'agent("noop")\nargs["new"] = 7\nreturn args["new"]'
    summary = run_workflow_program(
        source,
        dispatcher=_Dispatcher(),
        args={"x": 1},
    )
    assert summary.result == 7
    assert summary.status == "completed"
