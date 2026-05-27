"""Upstream output stitching for DAG child prompts."""

# ruff: noqa: TC001

from __future__ import annotations

from agent_fleet.orchestration.dag.schema import DagTask


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

    parts: list[str] = ["## Upstream task outputs"]
    for parent_id in task.depends_on:
        snippet = outputs.get(parent_id, "").strip()
        if not snippet:
            parts.append(f"### {parent_id}\n(no output recorded)")
            continue
        parts.append(f"### {parent_id}\n{truncate(snippet, max_chars_per_parent)}")
    return "\n\n".join(parts)


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
