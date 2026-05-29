"""Upstream output stitching for DAG child prompts."""

# ruff: noqa: TC001

from __future__ import annotations

from agent_fleet.orchestration.convergence import (
    FAILURE_STATUSES,
    PARTIAL_OK,
    SUCCESS_STATUSES,
    budget_upstream_context,
)
from agent_fleet.orchestration.dag.schema import DagTask

__all__ = [
    "FAILURE_STATUSES",
    "PARTIAL_OK",
    "SUCCESS_STATUSES",
    "build_dag_task_context",
    "build_upstream_context",
    "truncate",
]


def truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def build_upstream_context(
    task: DagTask,
    outputs: dict[str, str],
    *,
    max_chars_per_parent: int = 2000,
) -> str:
    """Build context block from completed upstream task summaries."""
    if not task.depends_on:
        return ""

    body = budget_upstream_context(
        outputs,
        task.depends_on,
        total_budget=max_chars_per_parent,
    )
    return f"## Upstream task outputs\n\n{body}"


def build_dag_task_context(
    task: DagTask,
    *,
    dag_title: str,
    parent_context: str = "",
    upstream_outputs: dict[str, str] | None = None,
    acceptance_criteria: list[str] | None = None,
    max_chars_per_parent: int = 2000,
) -> str:
    parts: list[str] = [f"DAG: {dag_title}", f"Node: {task.id}"]
    if parent_context.strip():
        parts.extend(["## Parent context", parent_context.strip()])
    if acceptance_criteria:
        parts.append("## Acceptance criteria")
        parts.extend(f"- {item}" for item in acceptance_criteria)
    if task.allowed_paths:
        parts.extend(["## Scope — only modify", ", ".join(task.allowed_paths)])
    upstream = build_upstream_context(
        task,
        upstream_outputs or {},
        max_chars_per_parent=max_chars_per_parent,
    )
    if upstream:
        parts.append(upstream)
    parts.extend(["## Task", task.subtask_prompt.strip()])
    return "\n\n".join(parts)
