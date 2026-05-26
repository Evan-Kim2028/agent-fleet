"""Build and run workstream task batches via the fleet dispatcher."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_fleet.dispatcher import dispatch_tasks
from agent_fleet.workstreams.scope import validate_parallel_batch

if TYPE_CHECKING:
    from agent_fleet.hooks import FleetTaskResult
    from agent_fleet.repo import RepoConfig
    from agent_fleet.workstreams.config import WorkstreamItem, WorkstreamsConfig


def build_workstream_context(
    *,
    repo: RepoConfig,
    config: WorkstreamsConfig,
    item: WorkstreamItem,
    parallel: bool,
) -> str:
    base = item.base_branch or config.base_branch or repo.default_branch
    target = item.target_branch or config.default_target_branch or repo.default_branch
    parts = [
        f"Workstream: {item.id}",
        f"Persona: {item.persona}",
    ]
    if config.plan:
        parts.append(f"Plan: {config.plan}")
    parts.extend(
        [
            f"Base branch: {base}",
            f"Target branch: {target}",
        ]
    )
    if parallel:
        parts.append("Parallel batch: stay within persona scope allowlist.")
    if item.context.strip():
        parts.append(item.context.strip())
    parts.append("Run pytest and ruff on touched files. Commit on target branch before finishing.")
    return "\n".join(parts)


def build_workstream_tasks(
    *,
    repo: RepoConfig,
    config: WorkstreamsConfig,
    item_ids: list[str],
    parallel: bool,
) -> list[dict[str, object]]:
    selected: list[WorkstreamItem] = []
    for item_id in item_ids:
        item = config.get(item_id)
        if item is None:
            raise ValueError(f"Unknown workstream id: {item_id!r}")
        selected.append(item)

    validate_parallel_batch(repo, selected, sequential_stack=config.sequential_stack and parallel)

    tasks: list[dict[str, object]] = []
    for item in selected:
        base = item.base_branch or config.base_branch or repo.default_branch
        pipeline = item.pipeline or config.pipeline
        tasks.append(
            {
                "goal": item.goal,
                "context": build_workstream_context(
                    repo=repo,
                    config=config,
                    item=item,
                    parallel=parallel,
                ),
                "persona": item.persona,
                "workspace": str(repo.repo_root),
                "pipeline": pipeline,
                "base_branch": base,
            }
        )
    return tasks


def run_workstreams(
    *,
    repo: RepoConfig,
    config: WorkstreamsConfig,
    item_ids: list[str],
    parallel: bool = False,
    fleet_config_path: str | None = None,
) -> list[FleetTaskResult]:
    """Dispatch one or more configured workstreams."""
    tasks = build_workstream_tasks(
        repo=repo,
        config=config,
        item_ids=item_ids,
        parallel=parallel,
    )
    if parallel:
        return dispatch_tasks(
            tasks=tasks,
            config_path=fleet_config_path,
            pipeline=config.pipeline,
        )

    results: list[FleetTaskResult] = []
    for task in tasks:
        batch = dispatch_tasks(
            tasks=[task],
            config_path=fleet_config_path,
            pipeline=str(task.get("pipeline") or config.pipeline),
        )
        results.extend(batch)
        last = batch[-1] if batch else None
        if last and last.status != "completed" and config.sequential_stack:
            break
    return results


def workstreams_status(repo: RepoConfig, config: WorkstreamsConfig) -> str:
    payload = {
        "plan": config.plan,
        "base_branch": config.base_branch or repo.default_branch,
        "default_target_branch": config.default_target_branch,
        "sequential_stack": config.sequential_stack,
        "pipeline": config.pipeline,
        "items": [
            {
                "id": item.id,
                "persona": item.persona,
                "target_branch": item.target_branch or config.default_target_branch,
                "base_branch": item.base_branch or config.base_branch or repo.default_branch,
            }
            for item in config.items
        ],
    }
    return json.dumps(payload, indent=2)
