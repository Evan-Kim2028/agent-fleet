"""Task execution helpers extracted from FleetDispatcher."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from agent_fleet.code_review import publish_fleet_branch, run_code_review_with_auto_fix
from agent_fleet.fleet_session import create_fleet_session
from agent_fleet.handoff_context import apply_handoff_to_task
from agent_fleet.phases import resolve_pipeline_outcome, run_pipeline
from agent_fleet.worktree import TaskWorkspace, prepare_task_workspace, should_isolate_worktree

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    from agent_fleet.config import FleetConfig
    from agent_fleet.contracts.handoff import HandoffNote
    from agent_fleet.hooks import FleetTask, FleetTaskResult, LLMBackend
    from agent_fleet.observability.fleet_logger import FleetLogger
    from agent_fleet.personas import YamlPersonaResolver
    from agent_fleet.repo import RepoConfig


def _stderr_from_phases(phase_results: list[dict[str, object]]) -> str:
    for item in reversed(phase_results):
        raw = item.get("stderr")
        if raw:
            return str(raw)
        err = item.get("error")
        if err:
            return str(err)
    return ""


def prepare_task_workspace_if_needed(
    *,
    task_index: int,
    workspace: Path,
    repo_config: RepoConfig | None,
    git_repo: RepoConfig | None,
    phases: list[str],
    batch_size: int,
    same_workspace_tasks: int,
    worktree_lock: threading.Lock,
    base_branch: str | None = None,
) -> tuple[Path, TaskWorkspace | None, str | None]:
    """Return (run_workspace, task_workspace, error_message)."""
    force_parallel = batch_size > 1 and same_workspace_tasks > 1
    isolate = phases != ["full"] and should_isolate_worktree(
        repo_config or git_repo,
        batch_size=batch_size,
        same_workspace_tasks=same_workspace_tasks,
    )
    if not isolate:
        return workspace, None, None

    if git_repo is None:
        return (
            workspace,
            None,
            (
                "Parallel fleet dispatch requires a git repository. "
                f"Initialize git in {workspace} or dispatch sequentially."
            ),
        )
    try:
        with worktree_lock:
            task_workspace = prepare_task_workspace(
                git_repo,
                task_index=task_index,
                force_isolation=force_parallel,
                base_branch=base_branch,
            )
    except RuntimeError as exc:
        return workspace, None, str(exc)
    return task_workspace.path, task_workspace, None


def run_configured_pipeline(
    *,
    backend: LLMBackend,
    resolver: YamlPersonaResolver,
    task: FleetTask,
    run_workspace: Path,
    task_config: FleetConfig,
    phases: list[str],
    repo_config: RepoConfig | None,
    git_repo: RepoConfig | None,
    handoff: HandoffNote | None,
) -> tuple[list[dict[str, object]], str, int, list[str] | None]:
    effective_task = apply_handoff_to_task(task, handoff)
    pipeline_name = task.pipeline or task_config.default_pipeline
    repo_for_publish = repo_config or git_repo
    code_review_cfg = repo_for_publish.code_review if repo_for_publish else None
    use_auto_fix = (
        pipeline_name == "code_review" and code_review_cfg is not None and code_review_cfg.auto_fix
    )

    session = (
        None
        if use_auto_fix
        else create_fleet_session(
            backend,
            fleet_config=task_config,
            persona_resolver=resolver,
            persona=effective_task.persona,
            cwd=run_workspace,
        )
    )
    try:
        if use_auto_fix:
            return run_code_review_with_auto_fix(
                backend=backend,
                resolver=resolver,
                task=effective_task,
                workspace=run_workspace,
                timeout_s=task_config.timeout_seconds,
                phases=phases,
                repo=repo_config or git_repo,
                config=code_review_cfg,
                fleet_config=task_config,
            )
        return run_pipeline(
            backend=backend,
            resolver=resolver,
            task=task,
            workspace=run_workspace,
            timeout_s=task_config.timeout_seconds,
            phases=phases,
            repo=repo_config or git_repo,
            session=session,
            fleet_config=task_config,
        )
    finally:
        if session is not None:
            session.dispose()


def build_task_result(
    *,
    task_index: int,
    task: FleetTask,
    start: float,
    phase_results: list[dict[str, object]],
    summary: str,
    exit_code: int,
    changed_files: list[str] | None,
    task_workspace: TaskWorkspace | None,
    fleet_log: FleetLogger,
) -> FleetTaskResult:
    from agent_fleet.hooks import FleetTaskResult

    agent_id: str | None = None
    for phase in reversed(phase_results):
        raw_id = phase.get("agent_id")
        if raw_id:
            agent_id = str(raw_id)
            break

    status, error = resolve_pipeline_outcome(phase_results, exit_code)
    stderr_tail = _stderr_from_phases(phase_results)
    files_modified = tuple(changed_files or ())

    result = FleetTaskResult(
        task_index=task_index,
        persona=task.persona,
        goal=task.goal,
        status=status,
        summary=summary or None,
        error=error,
        duration_seconds=round(time.monotonic() - start, 2),
        agent_id=agent_id,
        phases=_phase_map(phase_results),
        changed_files=changed_files,
        worktree=str(task_workspace.path) if task_workspace else None,
        branch_name=task_workspace.branch_name if task_workspace else None,
        stderr=stderr_tail,
        files_modified=files_modified,
    )
    fleet_log.emit(
        "fleet.task.complete",
        task_index=task_index,
        persona=task.persona,
        status=status,
        duration_seconds=result.duration_seconds,
    )
    return result


def record_completed_task_experience(
    *,
    task_index: int,
    task: FleetTask,
    status: str,
    phase_results: list[dict[str, object]],
    changed_files: list[str] | None,
    workspace: Path | None,
    fleet_log: FleetLogger,
) -> None:
    from agent_fleet.level_up.record import record_task_experience

    record_task_experience(
        task=task,
        status=status,
        phase_results=phase_results,
        changed_files=changed_files,
        workspace=workspace,
        run_id=fleet_log.run_id,
        task_index=task_index,
    )


def maybe_publish_and_pr_loop(
    *,
    status: str,
    task: FleetTask,
    run_workspace: Path,
    task_workspace: TaskWorkspace | None,
    repo_for_publish: RepoConfig,
    task_config: FleetConfig,
    code_review_cfg: object | None,
) -> tuple[str, str | None, int | None, str | None]:
    """Return (status, error, pr_number, pr_loop_status)."""
    pr_number: int | None = None
    pr_loop_status: str | None = None
    error: str | None = None
    if not (
        status == "completed"
        and code_review_cfg
        and getattr(code_review_cfg, "auto_push", False)
        and task_workspace
        and task_workspace.isolated
        and task_workspace.branch_name
    ):
        return status, error, pr_number, pr_loop_status

    pr_number = publish_fleet_branch(
        worktree=run_workspace,
        branch=task_workspace.branch_name,
        repo=repo_for_publish,
        task_goal=task.goal,
        persona=task.persona,
    )
    if (
        pr_number
        and getattr(code_review_cfg, "auto_pr_loop", False)
        and repo_for_publish.pr_loop
        and repo_for_publish.pr_loop.enabled
    ):
        from agent_fleet.pr_loop.lifecycle import run_pr_lifecycle

        loop_result = run_pr_lifecycle(
            pr_number=pr_number,
            branch=task_workspace.branch_name,
            repo=repo_for_publish,
            loop_config=repo_for_publish.pr_loop,
            fleet_config=task_config,
            worktree=run_workspace,
            skip_review_wait=False,
            persona=task.persona,
        )
        pr_loop_status = loop_result.status
        if loop_result.status == "merged":
            return "merged", None, pr_number, pr_loop_status
    return status, error, pr_number, pr_loop_status


def _phase_map(phase_results: list[dict[str, object]]) -> dict[str, object]:
    mapped: dict[str, object] = {}
    counts: dict[str, int] = {}
    for item in phase_results:
        base = str(item.get("phase", "?"))
        counts[base] = counts.get(base, 0) + 1
        key = base if counts[base] == 1 else f"{base}_{counts[base]}"
        mapped[key] = item
    return mapped
