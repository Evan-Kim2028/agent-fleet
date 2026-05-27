"""Terminal ASCII visualization for DAG specs."""

# ruff: noqa: TC001

from __future__ import annotations

from agent_fleet.orchestration.dag.schema import DagSpec, DagTask


def render_dag_ascii(
    spec: DagSpec,
    ranks: list[list[DagTask]],
    *,
    status_by_id: dict[str, str] | None = None,
) -> str:
    """Render a rank schedule and dependency edges for terminal output."""
    status_by_id = status_by_id or {}
    width = min(72, max(len(spec.title), 20))
    lines = [spec.title, "─" * width, ""]

    for index, rank in enumerate(ranks, start=1):
        parallel = len(rank) > 1
        label = "parallel" if parallel else "serial"
        lines.append(f"Rank {index} ({label})")
        for task in rank:
            deps = ", ".join(task.depends_on) if task.depends_on else "—"
            status = status_by_id.get(task.id)
            badge = f" [{status}]" if status else ""
            lines.append(f"  • {task.id}{badge}")
            lines.append(f"      deps: {deps}")
        lines.append("")

    lines.append("Edges")
    for task in spec.tasks:
        if not task.depends_on:
            continue
        for dep in task.depends_on:
            lines.append(f"  {dep} ──► {task.id}")

    return "\n".join(lines).rstrip() + "\n"
