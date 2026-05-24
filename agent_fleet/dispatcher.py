"""Parallel fleet dispatcher — silphcoanalytics admission + Hermes delegate_task."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.admission import AdmissionController, ResourceTier
from agent_fleet.backends import make_backend
from agent_fleet.code_review import publish_fleet_branch, run_code_review_with_auto_fix
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.hooks import FleetTask, FleetTaskResult, SessionCapableBackend
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.phases import resolve_pipeline_outcome, run_pipeline
from agent_fleet.redispatch import dispatch_with_retry
from agent_fleet.repo import RepoConfig, find_repo_config, merge_repo_into_fleet_config
from agent_fleet.runner import run_full_pipeline
from agent_fleet.worktree import TaskWorkspace, prepare_task_workspace, should_isolate_worktree

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.contracts.handoff import HandoffNote
    from agent_fleet.hooks import LLMSession

logger = logging.getLogger(__name__)


def _phase_map(phase_results: list[dict[str, object]]) -> dict[str, object]:
    mapped: dict[str, object] = {}
    counts: dict[str, int] = {}
    for item in phase_results:
        base = str(item.get("phase", "?"))
        counts[base] = counts.get(base, 0) + 1
        key = base if counts[base] == 1 else f"{base}_{counts[base]}"
        mapped[key] = item
    return mapped


def _optional_entry_str(value: object | None, fallback: str | None) -> str | None:
    if value is None:
        return fallback
    return str(value)


def _normalize_tasks(
    *,
    goal: str | None,
    context: str | None,
    persona: str | None,
    workspace: str | None,
    pipeline: str | None,
    tasks: list[dict[str, object]] | None,
) -> list[FleetTask]:
    if tasks:
        normalized: list[FleetTask] = []
        for entry in tasks:
            if not isinstance(entry, dict):
                continue
            task_goal = str(entry.get("goal") or "").strip()
            if not task_goal:
                continue
            normalized.append(
                FleetTask(
                    goal=task_goal,
                    context=str(entry.get("context") or ""),
                    persona=str(entry.get("persona") or persona or "coder"),
                    workspace=_optional_entry_str(entry.get("workspace"), workspace),
                    pipeline=_optional_entry_str(entry.get("pipeline"), pipeline),
                )
            )
        if normalized:
            return normalized
    if goal and goal.strip():
        return [
            FleetTask(
                goal=goal.strip(),
                context=str(context or ""),
                persona=str(persona or "coder"),
                workspace=workspace,
                pipeline=pipeline,
            )
        ]
    raise ValueError("Provide either 'goal' (single task) or 'tasks' (batch).")


class FleetDispatcher:
    def __init__(
        self,
        config: FleetConfig | None = None,
        *,
        progress_callback: Callable[..., None] | None = None,
    ) -> None:
        self.config = config or load_fleet_config()
        self.resolver = YamlPersonaResolver(self.config)
        self.backend = make_backend(self.config)
        self.progress_callback = progress_callback
        self._admission = AdmissionController(
            ram_budget_gb=self.config.ram_budget_gb,
            tiers={
                "agent": ResourceTier("agent", ram_gb=4, max_concurrent=self.config.max_parallel),
            },
        )
        self._admission_lock = threading.Lock()
        self._worktree_lock = threading.Lock()

    def _workspace_key(self, task: FleetTask) -> str:
        return str(self._resolve_workspace(task))

    def _workspace_task_counts(self, tasks: list[FleetTask]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in tasks:
            key = self._workspace_key(task)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _isolation_repo(self, workspace: Path) -> RepoConfig | None:
        repo_config = find_repo_config(workspace)
        git_root = repo_config.repo_root if repo_config else workspace
        from agent_fleet.verify_core import is_git_repo

        if not is_git_repo(git_root):
            return None
        if repo_config is not None:
            return repo_config
        return RepoConfig(repo_root=git_root)

    def _emit(self, event: str, **payload: object) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(event, **payload)
        except Exception as exc:
            logger.debug("Fleet progress callback failed: %s", exc)

    def _resolve_workspace(self, task: FleetTask) -> Path:
        raw = task.workspace or self.config.default_workspace or "."
        return Path(str(raw)).expanduser().resolve()

    def _resolve_pipeline(self, task: FleetTask) -> list[str]:
        name = task.pipeline or self.config.default_pipeline
        if name == "full":
            return ["full"]
        phases = self.config.pipelines.get(name)
        if not phases:
            raise ValueError(
                f"Unknown pipeline {name!r}. Available: {', '.join(sorted(self.config.pipelines))}"
            )
        return list(phases)

    def _run_full_pipeline(
        self, task_index: int, task: FleetTask, workspace: Path, start: float
    ) -> FleetTaskResult:
        repo = find_repo_config(workspace)
        config = merge_repo_into_fleet_config(self.config, repo)
        result = run_full_pipeline(
            goal=task.goal,
            context=task.context,
            title=task.title,
            persona=task.persona or config.default_persona,
            workspace=workspace,
            backend=self.backend,
            persona_resolver=self.resolver,
        )
        status = "completed" if result.outcome == "completed" else "error"
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status=status,
            summary=result.summary,
            error=result.error,
            duration_seconds=round(time.monotonic() - start, 2),
            phases=result.phases,
            task_spec=result.task_spec,
            changed_files=result.changed_files,
        )

    def _run_one(self, task: FleetTask, *, handoff: HandoffNote | None = None) -> FleetTaskResult:
        """Thin retry-aware wrapper called by dispatch_with_retry.

        ``handoff`` carries context from a previous failed attempt.  It is
        forwarded to ``_execute_task`` where it will be consumed once the
        runner grows handoff support (Task 5).  For now it is accepted and
        logged but otherwise unused downstream.
        """
        if handoff is not None:
            logger.debug(
                "Redispatch attempt #%s for task %r (failure_mode=%r)",
                handoff.attempt_number,
                task.goal[:60],
                handoff.failure_mode,
            )
        return self._execute_task(0, task, batch_size=1, same_workspace_tasks=1, handoff=handoff)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,
        same_workspace_tasks: int = 1,
        handoff: HandoffNote | None = None,  # noqa: ARG002 — wired by Task 5
    ) -> FleetTaskResult:
        start = time.monotonic()
        self._emit(
            "fleet.task.start",
            task_index=task_index,
            persona=task.persona,
            goal=task.goal[:120],
        )

        token = None
        task_workspace: TaskWorkspace | None = None
        with self._admission_lock:
            token = self._admission.try_admit("agent")
        if token is None:
            return FleetTaskResult(
                task_index=task_index,
                persona=task.persona,
                goal=task.goal,
                status="error",
                summary=None,
                error="Fleet admission denied (max parallel agents reached)",
                duration_seconds=round(time.monotonic() - start, 2),
            )

        try:
            workspace = self._resolve_workspace(task)
            if not workspace.exists():
                return FleetTaskResult(
                    task_index=task_index,
                    persona=task.persona,
                    goal=task.goal,
                    status="error",
                    summary=None,
                    error=f"Workspace does not exist: {workspace}",
                    duration_seconds=round(time.monotonic() - start, 2),
                )

            repo_config = find_repo_config(workspace)
            git_repo = self._isolation_repo(workspace)

            phases = self._resolve_pipeline(task)
            force_parallel = batch_size > 1 and same_workspace_tasks > 1
            isolate = phases != ["full"] and should_isolate_worktree(
                repo_config or git_repo,
                batch_size=batch_size,
                same_workspace_tasks=same_workspace_tasks,
            )
            if isolate:
                if git_repo is None:
                    return FleetTaskResult(
                        task_index=task_index,
                        persona=task.persona,
                        goal=task.goal,
                        status="error",
                        summary=None,
                        error=(
                            "Parallel fleet dispatch requires a git repository. "
                            f"Initialize git in {workspace} or dispatch sequentially."
                        ),
                        duration_seconds=round(time.monotonic() - start, 2),
                    )
                try:
                    with self._worktree_lock:
                        task_workspace = prepare_task_workspace(
                            git_repo,
                            task_index=task_index,
                            force_isolation=force_parallel,
                        )
                except RuntimeError as exc:
                    return FleetTaskResult(
                        task_index=task_index,
                        persona=task.persona,
                        goal=task.goal,
                        status="error",
                        summary=None,
                        error=str(exc),
                        duration_seconds=round(time.monotonic() - start, 2),
                    )

            run_workspace = task_workspace.path if task_workspace else workspace
            task_config = merge_repo_into_fleet_config(
                self.config,
                repo_config or git_repo,
            )
            resolver = YamlPersonaResolver(task_config)

            if phases == ["full"]:
                result = self._run_full_pipeline(task_index, task, run_workspace, start)
                self._emit(
                    "fleet.task.complete",
                    task_index=task_index,
                    persona=task.persona,
                    status=result.status,
                    duration_seconds=result.duration_seconds,
                )
                if task_workspace is not None:
                    task_workspace.teardown(keep=result.status == "completed")
                return result

            phase_results: list[dict[str, object]] = []
            if task_workspace is not None and task_workspace.isolated:
                phase_results.append(
                    {
                        "phase": "worktree",
                        "path": str(task_workspace.path),
                        "branch": task_workspace.branch_name,
                        "run_id": task_workspace.run_id,
                    }
                )

            pipeline_name = task.pipeline or task_config.default_pipeline
            repo_for_publish = repo_config or git_repo
            code_review_cfg = repo_for_publish.code_review if repo_for_publish else None
            use_auto_fix = (
                pipeline_name == "code_review"
                and code_review_cfg is not None
                and code_review_cfg.auto_fix
            )

            session: LLMSession | None = None
            if isinstance(self.backend, SessionCapableBackend) and not use_auto_fix:
                persona_spec = resolver.load(task.persona)
                mcp_specs = {
                    name: task_config.mcp_servers[name]
                    for name in (getattr(persona_spec, "mcp_servers", []) or [])
                    if name in task_config.mcp_servers
                }
                session = self.backend.create_session(
                    persona_name=task.persona,
                    cwd=run_workspace,
                    mcp_servers=mcp_specs,
                    model=persona_spec.model,
                    mode=persona_spec.mode,
                )

            try:
                if use_auto_fix:
                    (
                        pipeline_results,
                        summary,
                        exit_code,
                        changed_files,
                    ) = run_code_review_with_auto_fix(
                        backend=self.backend,
                        resolver=resolver,
                        task=task,
                        workspace=run_workspace,
                        timeout_s=task_config.timeout_seconds,
                        phases=phases,
                        repo=repo_config or git_repo,
                        config=code_review_cfg,
                    )
                else:
                    pipeline_results, summary, exit_code, changed_files = run_pipeline(
                        backend=self.backend,
                        resolver=resolver,
                        task=task,
                        workspace=run_workspace,
                        timeout_s=task_config.timeout_seconds,
                        phases=phases,
                        repo=repo_config or git_repo,
                        session=session,
                    )
            finally:
                if session is not None:
                    session.dispose()
            phase_results.extend(pipeline_results)
            agent_id: str | None = None
            for phase in reversed(phase_results):
                raw_id = phase.get("agent_id")
                if raw_id:
                    agent_id = str(raw_id)
                    break

            status, error = resolve_pipeline_outcome(phase_results, exit_code)

            pr_number: int | None = None
            pr_loop_status: str | None = None
            repo_for_publish = repo_config or git_repo
            if (
                status == "completed"
                and code_review_cfg
                and code_review_cfg.auto_push
                and task_workspace
                and task_workspace.isolated
                and task_workspace.branch_name
                and repo_for_publish
            ):
                pr_number = publish_fleet_branch(
                    worktree=run_workspace,
                    branch=task_workspace.branch_name,
                    repo=repo_for_publish,
                    task_goal=task.goal,
                    persona=task.persona,
                )
                if (
                    pr_number
                    and code_review_cfg.auto_pr_loop
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
                        status = "merged"
                        error = None

            keep_worktree = status in {"completed", "merged"} or (
                code_review_cfg is not None
                and code_review_cfg.auto_push
                and task_workspace is not None
                and task_workspace.isolated
            )

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
                changed_files=changed_files or None,
                worktree=str(task_workspace.path) if task_workspace else None,
                branch_name=task_workspace.branch_name if task_workspace else None,
                pr_number=pr_number,
                pr_loop_status=pr_loop_status,
            )
            self._emit(
                "fleet.task.complete",
                task_index=task_index,
                persona=task.persona,
                status=status,
                duration_seconds=result.duration_seconds,
            )
            if task_workspace is not None:
                task_workspace.teardown(keep=keep_worktree)
            return result
        except Exception as exc:
            logger.exception("Fleet task %s failed", task_index)
            if task_workspace is not None:
                task_workspace.teardown(keep=False)
            return FleetTaskResult(
                task_index=task_index,
                persona=task.persona,
                goal=task.goal,
                status="error",
                summary=None,
                error=str(exc),
                duration_seconds=round(time.monotonic() - start, 2),
            )
        finally:
            if token is not None:
                with self._admission_lock:
                    self._admission.release(token)

    def dispatch(
        self,
        *,
        goal: str | None = None,
        context: str | None = None,
        persona: str | None = None,
        workspace: str | None = None,
        pipeline: str | None = None,
        tasks: list[dict[str, object]] | None = None,
    ) -> list[FleetTaskResult]:
        normalized = _normalize_tasks(
            goal=goal,
            context=context,
            persona=persona,
            workspace=workspace,
            pipeline=pipeline,
            tasks=tasks,
        )
        if len(normalized) > self.config.max_parallel:
            raise ValueError(
                f"Too many tasks ({len(normalized)}). max_parallel={self.config.max_parallel}"
            )

        batch_size = len(normalized)
        workspace_counts = self._workspace_task_counts(normalized)

        if batch_size == 1:
            task = normalized[0]

            def _run_with_handoff(
                t: FleetTask,
                *,
                handoff: HandoffNote | None = None,
            ) -> FleetTaskResult:
                return self._run_one(t, handoff=handoff)

            return [
                dispatch_with_retry(
                    task,
                    dispatch=_run_with_handoff,
                    max_redispatches=self.config.max_redispatches,
                    on_event=lambda evt, payload: self._emit(evt, **payload),
                )
            ]

        results: list[FleetTaskResult | None] = [None] * batch_size
        with ThreadPoolExecutor(max_workers=batch_size) as pool:
            futures = {
                pool.submit(
                    self._execute_task,
                    idx,
                    task,
                    batch_size=batch_size,
                    same_workspace_tasks=workspace_counts[self._workspace_key(task)],
                ): idx
                for idx, task in enumerate(normalized)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
        return [r for r in results if r is not None]


def dispatch_tasks(
    *,
    goal: str | None = None,
    context: str | None = None,
    persona: str | None = None,
    workspace: str | None = None,
    pipeline: str | None = None,
    tasks: list[dict[str, object]] | None = None,
    config_path: str | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> list[FleetTaskResult]:
    config = load_fleet_config(config_path) if config_path else load_fleet_config()
    dispatcher = FleetDispatcher(config=config, progress_callback=progress_callback)
    return dispatcher.dispatch(
        goal=goal,
        context=context,
        persona=persona,
        workspace=workspace,
        pipeline=pipeline,
        tasks=tasks,
    )
