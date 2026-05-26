"""Parallel fleet dispatcher — silphcoanalytics admission + Hermes delegate_task."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.admission import AdmissionController, ResourceTier
from agent_fleet.backends import make_backend
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.dispatcher_task import (
    build_task_result,
    maybe_publish_and_pr_loop,
    prepare_task_workspace_if_needed,
    record_completed_task_experience,
    run_configured_pipeline,
)
from agent_fleet.handoff_context import apply_handoff_to_task
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.observability.fleet_logger import FleetLogger
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.redispatch import dispatch_with_retry
from agent_fleet.repo import RepoConfig, find_repo_config, merge_repo_into_fleet_config
from agent_fleet.runner import run_full_pipeline
from agent_fleet.verify_core import is_git_repo
from agent_fleet.worktree import maybe_commit_recoverable_worktree, should_keep_task_worktree

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.contracts.handoff import HandoffNote
    from agent_fleet.orchestration.config import OrchestrationConfig
    from agent_fleet.spine_config import SpineConfig

logger = logging.getLogger(__name__)


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
) -> tuple[list[FleetTask], list[str | None]]:
    base_branches: list[str | None] = []
    if tasks:
        normalized: list[FleetTask] = []
        for entry in tasks:
            if not isinstance(entry, dict):
                continue
            task_goal = str(entry.get("goal") or "").strip()
            if not task_goal:
                continue
            base_branches.append(_optional_entry_str(entry.get("base_branch"), None))
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
            return normalized, base_branches
    if goal and goal.strip():
        return [
            FleetTask(
                goal=goal.strip(),
                context=str(context or ""),
                persona=str(persona or "coder"),
                workspace=workspace,
                pipeline=pipeline,
            )
        ], [None]
    raise ValueError("Provide either 'goal' (single task) or 'tasks' (batch).")


def _scope_prefixes_for_persona(repo: RepoConfig, persona: str) -> frozenset[str]:
    allowlist = repo.persona_scope_allowlist.get(persona)
    if not allowlist:
        return frozenset()
    return frozenset(str(prefix).rstrip("/") for prefix in allowlist)


def _scope_prefixes_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


def _warn_parallel_scope_overlap(repo: RepoConfig, tasks: list[FleetTask]) -> None:
    """Log when parallel batch personas share scope allowlist prefixes."""
    personas = [task.persona for task in tasks]
    overlaps: list[str] = []
    for i, persona_a in enumerate(personas):
        prefixes_a = _scope_prefixes_for_persona(repo, persona_a)
        for persona_b in personas[i + 1 :]:
            prefixes_b = _scope_prefixes_for_persona(repo, persona_b)
            for prefix_a in prefixes_a:
                for prefix_b in prefixes_b:
                    if _scope_prefixes_overlap(prefix_a, prefix_b):
                        overlaps.append(f"{persona_a} ↔ {persona_b} (prefix {prefix_a!r})")
                        break
    if overlaps:
        logger.warning(
            "Parallel batch may collide on shared persona scope prefixes: %s",
            "; ".join(overlaps[:5]),
        )


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
        if not is_git_repo(git_root):
            return None
        if repo_config is not None:
            return repo_config
        return RepoConfig(repo_root=git_root)

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

    def _resolve_orchestration(self, repo: RepoConfig | None) -> OrchestrationConfig:
        from agent_fleet.orchestration.config import resolve_orchestration_config

        if repo is not None and repo.orchestration is not None:
            return repo.orchestration
        return resolve_orchestration_config(None)

    def _spine_from_repo(self, repo: RepoConfig | None) -> SpineConfig:
        from agent_fleet.runner import _spine_from_repo

        return _spine_from_repo(repo)

    def _maybe_preflight_and_dispatch(
        self,
        *,
        task_index: int,
        task: FleetTask,
        workspace: Path,
        repo_config: RepoConfig | None,
        git_repo: RepoConfig | None,
        task_config: FleetConfig,
        resolver: YamlPersonaResolver,
        start: float,
        fleet_log: FleetLogger,
        handoff: HandoffNote | None,
    ) -> tuple[FleetTaskResult | None, FleetTask]:
        """Run plan preflight; return (early_result, task_to_run)."""
        from agent_fleet.fleet_session import create_fleet_session
        from agent_fleet.orchestration.decompose import (
            coerce_empty_decompose,
            dispatch_task_spec_children,
            enrich_task_from_task_spec,
            handle_preflight_decision,
            preflight_plan,
        )

        repo = repo_config or git_repo
        orchestration = self._resolve_orchestration(repo)
        if not orchestration.enabled or not orchestration.preflight_on_code_review:
            return None, task
        if handoff is not None:
            return None, task

        pipeline_name = task.pipeline or task_config.default_pipeline
        if pipeline_name in {"full", "pr_review"}:
            return None, task

        spine = self._spine_from_repo(repo)
        session = create_fleet_session(
            self.backend,
            fleet_config=task_config,
            persona_resolver=resolver,
            persona=task.persona,
            cwd=workspace,
        )
        task_id = int(time.time()) % 100000
        try:
            task_spec = preflight_plan(
                task=task,
                task_id=task_id,
                backend=self.backend,
                persona_resolver=resolver,
                spine_config=spine,
                session=session,
            )
        except Exception as exc:
            logger.warning("Plan preflight failed; continuing without plan: %s", exc)
            return None, task
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    session.dispose()

        task_spec, decompose_fallback = coerce_empty_decompose(task_spec)
        if decompose_fallback:
            fleet_log.emit(
                "orchestration.decompose_fallback",
                data={"reason": "empty child_issues_proposed"},
            )

        if (
            task_spec.decomposition_decision.value == "decompose"
            and orchestration.auto_dispatch_children
        ):
            child_results, status, error, summary = dispatch_task_spec_children(
                task_spec=task_spec,
                parent_task=task,
                dispatcher=self,
                child_pipeline=orchestration.default_child_pipeline,
                persona_resolver=resolver,
                fallback_persona=task.persona or task_config.default_persona,
                parent_run_id=fleet_log.run_id,
            )
            fleet_log.emit(
                "orchestration.decompose",
                child_count=len(child_results),
                status=status,
            )
            return (
                FleetTaskResult(
                    task_index=task_index,
                    persona=task.persona,
                    goal=task.goal,
                    status=status,
                    summary=summary,
                    error=error,
                    duration_seconds=round(time.monotonic() - start, 2),
                    phases={
                        "plan": task_spec.to_dict(),
                        "decompose_dispatch": [r.__dict__ for r in child_results],
                    },
                    task_spec=task_spec.to_dict(),
                ),
                task,
            )

        preflight_status, preflight_error = handle_preflight_decision(task_spec)
        if preflight_status in {"rejected", "decompose"}:
            return (
                FleetTaskResult(
                    task_index=task_index,
                    persona=task.persona,
                    goal=task.goal,
                    status=preflight_status,
                    summary=task_spec.decomposition_reason,
                    error=preflight_error,
                    duration_seconds=round(time.monotonic() - start, 2),
                    phases={"plan": task_spec.to_dict()},
                    task_spec=task_spec.to_dict(),
                ),
                task,
            )

        return None, enrich_task_from_task_spec(task, task_spec)

    def _run_full_pipeline(
        self,
        task_index: int,
        task: FleetTask,
        workspace: Path,
        start: float,
        *,
        handoff: HandoffNote | None,
        fleet_log: FleetLogger,
    ) -> FleetTaskResult:
        repo = find_repo_config(workspace)
        config = merge_repo_into_fleet_config(self.config, repo)
        effective = apply_handoff_to_task(task, handoff)
        result = run_full_pipeline(
            goal=effective.goal,
            context=effective.context,
            title=task.title,
            persona=effective.persona or config.default_persona,
            workspace=workspace,
            backend=self.backend,
            persona_resolver=self.resolver,
            fleet_config=config,
        )
        ok_outcomes = {
            "completed",
            "completed_noop",
            "review_changes_requested",
            "decompose_partial",
        }
        status = "completed" if result.outcome in ok_outcomes else "error"
        if result.outcome in {"decompose_failed", "rejected", "decompose"}:
            status = result.outcome
        fleet_log.emit(
            "fleet.task.complete",
            task_index=task_index,
            persona=task.persona,
            status=status,
            duration_seconds=round(time.monotonic() - start, 2),
        )
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
            stderr=result.error or "",
            files_modified=tuple(result.changed_files or ()),
        )

    def _run_one(self, task: FleetTask, *, handoff: HandoffNote | None = None) -> FleetTaskResult:
        return self._execute_task(0, task, batch_size=1, same_workspace_tasks=1, handoff=handoff)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,
        same_workspace_tasks: int = 1,
        handoff: HandoffNote | None = None,
        base_branch: str | None = None,
    ) -> FleetTaskResult:
        start = time.monotonic()
        fleet_log = FleetLogger.for_dispatch(
            task_index=task_index,
            persona=task.persona,
            progress_callback=self.progress_callback,
        )

        with fleet_log.bind():
            fleet_log.emit(
                "fleet.task.start",
                task_index=task_index,
                persona=task.persona,
                goal=task.goal[:120],
                has_handoff=handoff is not None,
            )
            if handoff is not None:
                fleet_log.emit(
                    "redispatch.handoff",
                    attempt=handoff.attempt_number,
                    failure_mode=handoff.failure_mode,
                )

            token = None
            task_workspace = None
            with self._admission_lock:
                token = self._admission.try_admit("agent")
            if token is None:
                fleet_log.emit("admission.denied", reason="max_parallel")
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
                run_workspace = workspace
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

                if phases == ["full"]:
                    return self._run_full_pipeline(
                        task_index, task, workspace, start, handoff=handoff, fleet_log=fleet_log
                    )

                run_workspace, task_workspace, wt_error = prepare_task_workspace_if_needed(
                    task_index=task_index,
                    workspace=workspace,
                    repo_config=repo_config,
                    git_repo=git_repo,
                    phases=phases,
                    batch_size=batch_size,
                    same_workspace_tasks=same_workspace_tasks,
                    worktree_lock=self._worktree_lock,
                    base_branch=base_branch,
                )
                if wt_error:
                    return FleetTaskResult(
                        task_index=task_index,
                        persona=task.persona,
                        goal=task.goal,
                        status="error",
                        summary=None,
                        error=wt_error,
                        duration_seconds=round(time.monotonic() - start, 2),
                    )

                task_config = merge_repo_into_fleet_config(
                    self.config,
                    repo_config or git_repo,
                )
                resolver = YamlPersonaResolver(task_config)

                preflight_result, task = self._maybe_preflight_and_dispatch(
                    task_index=task_index,
                    task=task,
                    workspace=workspace,
                    repo_config=repo_config,
                    git_repo=git_repo,
                    task_config=task_config,
                    resolver=resolver,
                    start=start,
                    fleet_log=fleet_log,
                    handoff=handoff,
                )
                if preflight_result is not None:
                    return preflight_result

                from agent_fleet.orchestration.equip import resolve_dispatch_equip

                equip = resolve_dispatch_equip(
                    task,
                    task_config,
                    repo_config or git_repo,
                    run_id=fleet_log.run_id,
                )
                task = replace(task, equip=equip)

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

                pipeline_results, summary, exit_code, changed_files = run_configured_pipeline(
                    backend=self.backend,
                    resolver=resolver,
                    task=task,
                    run_workspace=run_workspace,
                    task_config=task_config,
                    phases=phases,
                    repo_config=repo_config,
                    git_repo=git_repo,
                    handoff=handoff,
                )
                phase_results.extend(pipeline_results)

                result = build_task_result(
                    task_index=task_index,
                    task=task,
                    start=start,
                    phase_results=phase_results,
                    summary=summary,
                    exit_code=exit_code,
                    changed_files=changed_files,
                    task_workspace=task_workspace,
                    fleet_log=fleet_log,
                )

                repo_for_publish = repo_config or git_repo
                code_review_cfg = repo_for_publish.code_review if repo_for_publish else None
                pr_number = result.pr_number
                pr_loop_status = result.pr_loop_status
                status = result.status
                error = result.error
                if repo_for_publish is not None:
                    status, error, pr_number, pr_loop_status = maybe_publish_and_pr_loop(
                        status=result.status,
                        task=task,
                        run_workspace=run_workspace,
                        task_workspace=task_workspace,
                        repo_for_publish=repo_for_publish,
                        task_config=task_config,
                        code_review_cfg=code_review_cfg,
                    )
                if pr_number or pr_loop_status or status != result.status:
                    result = replace(
                        result,
                        status=status,
                        error=error,
                        pr_number=pr_number,
                        pr_loop_status=pr_loop_status,
                    )

                record_completed_task_experience(
                    task_index=task_index,
                    task=task,
                    status=result.status,
                    phase_results=phase_results,
                    changed_files=result.changed_files,
                    workspace=run_workspace,
                    fleet_log=fleet_log,
                )

                keep_worktree = should_keep_task_worktree(
                    result.status,
                    auto_push=bool(code_review_cfg and code_review_cfg.auto_push),
                    isolated=bool(task_workspace and task_workspace.isolated),
                    has_changes=bool(result.changed_files or result.files_modified),
                )
                if task_workspace is not None:
                    maybe_commit_recoverable_worktree(
                        task_workspace,
                        result.status,
                        goal=task.goal,
                    )
                    task_workspace.teardown(keep=keep_worktree)
                return result
            except Exception as exc:
                logger.exception("Fleet task %s failed", task_index)
                if task_workspace is not None:
                    task_workspace.teardown(keep=False)
                fleet_log.emit("fleet.task.error", error=str(exc))
                record_completed_task_experience(
                    task_index=task_index,
                    task=task,
                    status="error",
                    phase_results=phase_results,
                    changed_files=None,
                    workspace=run_workspace,
                    fleet_log=fleet_log,
                )
                return FleetTaskResult(
                    task_index=task_index,
                    persona=task.persona,
                    goal=task.goal,
                    status="error",
                    summary=None,
                    error=str(exc),
                    duration_seconds=round(time.monotonic() - start, 2),
                    stderr=str(exc),
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
        normalized, base_branches = _normalize_tasks(
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

        if batch_size > 1:
            first_repo = find_repo_config(self._resolve_workspace(normalized[0]))
            if first_repo is not None:
                _warn_parallel_scope_overlap(first_repo, normalized)

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
                    on_event=self._emit_progress,
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
                    base_branch=base_branches[idx] if idx < len(base_branches) else None,
                ): idx
                for idx, task in enumerate(normalized)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
        return [r for r in results if r is not None]

    def _emit_progress(self, event: str, payload: dict[str, object]) -> None:
        from agent_fleet.observability.context import get_run_log

        run_log = get_run_log()
        if run_log is not None:
            run_log.emit(event, data=payload)
        if self.progress_callback:
            try:
                self.progress_callback(event, **payload)
            except Exception as exc:
                logger.debug("Fleet progress callback failed: %s", exc)


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
