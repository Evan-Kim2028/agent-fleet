"""Topological rank scheduling for DAG tasks."""

# ruff: noqa: TC001

from __future__ import annotations

from agent_fleet.orchestration.dag.schema import DagSpec, DagTask


def validate_dag_graph(spec: DagSpec) -> None:
    """Validate ids, dependency references, and absence of cycles."""
    ids = spec.task_ids()
    if len(ids) != len(spec.tasks):
        raise ValueError("DAG tasks must have unique ids")

    for task in spec.tasks:
        for dep in task.depends_on:
            if dep not in ids:
                raise ValueError(f"Task {task.id!r} depends on unknown id {dep!r}")
            if dep == task.id:
                raise ValueError(f"Task {task.id!r} cannot depend on itself")

    ranks = topo_sort_ranks(spec.tasks)
    if sum(len(rank) for rank in ranks) != len(spec.tasks):
        raise ValueError("DAG contains a cycle")


def topo_sort_ranks(tasks: tuple[DagTask, ...]) -> list[list[DagTask]]:
    """Kahn topo-sort into parallel ranks (tasks in a rank may run concurrently)."""
    by_id = {task.id: task for task in tasks}
    remaining_deps: dict[str, set[str]] = {task.id: set(task.depends_on) for task in tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dep in task.depends_on:
            dependents[dep].append(task.id)

    ready = [task.id for task in tasks if not remaining_deps[task.id]]
    ranks: list[list[DagTask]] = []

    while ready:
        ready.sort()
        rank = [by_id[task_id] for task_id in ready]
        ranks.append(rank)
        next_ready: list[str] = []
        for task_id in ready:
            for child_id in dependents[task_id]:
                remaining_deps[child_id].discard(task_id)
                if not remaining_deps[child_id]:
                    next_ready.append(child_id)
        ready = next_ready

    if any(remaining_deps[task_id] for task_id in remaining_deps):
        raise ValueError("DAG contains a cycle")

    return ranks


def transitive_dependents(spec: DagSpec, failed_ids: set[str]) -> set[str]:
    """Return task ids that depend (directly or indirectly) on any failed id."""
    dependents: dict[str, set[str]] = {task.id: set() for task in spec.tasks}
    for task in spec.tasks:
        for dep in task.depends_on:
            dependents[dep].add(task.id)

    skipped: set[str] = set()
    queue = list(failed_ids)
    while queue:
        node = queue.pop()
        for child in dependents.get(node, ()):
            if child not in skipped:
                skipped.add(child)
                queue.append(child)
    return skipped
