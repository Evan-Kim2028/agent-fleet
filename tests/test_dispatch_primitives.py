"""Unit tests for DispatchPrimitives."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.primitives import DispatchPrimitives


@dataclass
class RecordingDispatcher:
    calls: list[tuple[int, int, int]] = field(default_factory=list)
    max_delay: float = 0.0

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,
        same_workspace_tasks: int = 1,
        handoff: object = None,  # noqa: ARG002
        base_branch: str | None = None,  # noqa: ARG002
        depth: int = 1,  # noqa: ARG002
    ) -> FleetTaskResult:
        self.calls.append((task_index, batch_size, same_workspace_tasks))
        if self.max_delay:
            time.sleep(self.max_delay)
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=None,
            error=None,
            duration_seconds=0.1,
        )


def _task(name: str) -> FleetTask:
    return FleetTask(goal=name, context="", persona="coder", workspace="/tmp")


def test_run_many_preserves_order() -> None:
    dispatcher = RecordingDispatcher()
    primitives = DispatchPrimitives(dispatcher, max_parallel=4)
    tasks = [_task("a"), _task("b"), _task("c")]
    results = primitives.run_many(tasks)
    assert [r.goal for r in results] == ["a", "b", "c"]
    assert sorted(c[0] for c in dispatcher.calls) == [0, 1, 2]


def test_run_many_wave_batch_size_and_same_workspace() -> None:
    dispatcher = RecordingDispatcher()
    primitives = DispatchPrimitives(dispatcher, max_parallel=2)
    tasks = [_task(f"t{i}") for i in range(3)]
    primitives.run_many(tasks)
    # wave1 runs in parallel so its two calls can land in either order
    assert sorted(dispatcher.calls[:2]) == [(0, 2, 1), (1, 2, 1)]
    assert dispatcher.calls[2] == (0, 1, 1)


def test_run_many_parallelism() -> None:
    dispatcher = RecordingDispatcher(max_delay=0.05)
    primitives = DispatchPrimitives(dispatcher, max_parallel=3)
    tasks = [_task(f"t{i}") for i in range(3)]
    start = time.monotonic()
    primitives.run_many(tasks)
    elapsed = time.monotonic() - start
    assert elapsed < 0.12


def test_run_one_batch_size_override() -> None:
    dispatcher = RecordingDispatcher()
    primitives = DispatchPrimitives(dispatcher, max_parallel=1)
    primitives.run_one(5, _task("solo"), batch_size=9)
    assert dispatcher.calls == [(5, 9, 1)]
