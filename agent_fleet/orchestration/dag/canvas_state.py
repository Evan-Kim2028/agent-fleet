"""Canvas RunState — JSON shape expected by the cookbook canvas template."""

# ruff: noqa: TC001

from __future__ import annotations

import time
from typing import Any

from agent_fleet.orchestration.dag.schema import DagSpec, DagTask

_DEFAULT_MODELS: dict[str, str] = {
    "HIGH": "composer-2.5",
    "MED": "composer-2.5",
    "LOW": "composer-2.5-fast",
}

_CANVAS_STATUS = frozenset({"PENDING", "RUNNING", "FINISHED", "ERROR"})


def model_for_task(task: DagTask, models: dict[str, str]) -> str:
    return models.get(task.complexity) or _DEFAULT_MODELS.get(task.complexity, "composer-2.5")


def fleet_status_to_canvas(status: str) -> str:
    if status in {"completed", "merged", "review_changes_requested"}:
        return "FINISHED"
    if status == "skipped":
        return "ERROR"
    return "ERROR"


def initial_run_state(spec: DagSpec) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "title": spec.title,
        "startedAt": now,
        "tasks": [
            {
                "id": task.id,
                "depends_on": list(task.depends_on),
                "complexity": task.complexity,
                "subtask_prompt": task.subtask_prompt,
                "status": "PENDING",
                "model": model_for_task(task, spec.models),
            }
            for task in spec.tasks
        ],
    }


def task_state_by_id(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        return {}
    return {str(t["id"]): t for t in tasks if isinstance(t, dict) and "id" in t}


def set_task_status(
    state: dict[str, Any],
    task_id: str,
    *,
    status: str,
    result_text: str | None = None,
    error_message: str | None = None,
    duration_ms: int | None = None,
) -> None:
    if status not in _CANVAS_STATUS:
        raise ValueError(f"Invalid canvas status: {status!r}")
    task = task_state_by_id(state).get(task_id)
    if task is None:
        return
    now = int(time.time() * 1000)
    task["status"] = status
    if status == "RUNNING":
        task["startedAt"] = now
    if status in {"FINISHED", "ERROR"}:
        task["finishedAt"] = now
        if duration_ms is not None:
            task["durationMs"] = duration_ms
        elif task.get("startedAt") is not None:
            task["durationMs"] = now - int(task["startedAt"])
    if result_text is not None:
        task["resultText"] = result_text[:4000]
    if error_message is not None:
        task["errorMessage"] = error_message


def finalize_run_state(
    state: dict[str, Any],
    *,
    outcome: str,
    message: str | None = None,
) -> None:
    state["finishedAt"] = int(time.time() * 1000)
    state["runOutcome"] = outcome
    if message:
        state["runMessage"] = message
