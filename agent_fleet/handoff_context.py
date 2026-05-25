"""Apply redispatch handoff notes to fleet task prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.hooks import FleetTask

if TYPE_CHECKING:
    from agent_fleet.contracts.handoff import HandoffNote


def apply_handoff_to_task(task: FleetTask, handoff: HandoffNote | None) -> FleetTask:
    if handoff is None:
        return task
    prefix = handoff.render()
    merged = f"{prefix}\n\n{task.context}".strip() if task.context.strip() else prefix
    return FleetTask(
        goal=task.goal,
        context=merged,
        persona=task.persona,
        workspace=task.workspace,
        pipeline=task.pipeline,
        title=task.title,
        equip=task.equip,
    )


def append_handoff_to_body(body: str, handoff: HandoffNote | None) -> str:
    if handoff is None:
        return body
    return f"{handoff.render()}\n\n{body}"
