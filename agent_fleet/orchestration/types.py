"""Shared orchestration types."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agent_fleet.hooks import FleetTask, FleetTaskResult


class _DispatcherLike(Protocol):
    """Structural type for objects that can execute a single fleet task."""

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = ...,
        same_workspace_tasks: int = ...,
        handoff: object = ...,
        base_branch: str | None = ...,
    ) -> FleetTaskResult: ...
