"""Parallel fleet dispatcher — inspired by silphcoanalytics fleet admission + Hermes delegate_task."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from agent_fleet.admission import AdmissionController, ResourceTier
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.repo import find_repo_config, merge_repo_into_fleet_config
from agent_fleet.runner import run_full_pipeline
from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.phases import run_pipeline

logger = logging.getLogger(__name__)


def _normalize_tasks(
    *,
    goal: str | None,
    context: str | None,
    persona: str | None,
    workspace: str | None,
    pipeline: str | None,
    tasks: list[dict[str, Any]] | None,
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
                    workspace=entry.get("workspace") or workspace,
                    pipeline=entry.get("pipeline") or pipeline,
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
        self.backend = CursorBackend(
            default_model=self.config.default_model,
            default_mode=self.config.default_mode,
        )
        self.progress_callback = progress_callback
        self._admission = AdmissionController(
            ram_budget_gb=self.config.ram_budget_gb,
            tiers={
                "agent": ResourceTier("agent", ram_gb=4, max_concurrent=self.config.max_parallel),
            },
        )
        self._admission_lock = threading.Lock()

    def _emit(self, event: str, **payload: Any) -> None:
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

    def _run_full_pipeline(self, task_index: int, task: FleetTask, workspace: Path, start: float) -> FleetTaskResult:
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

    def _run_one(self, task_index: int, task: FleetTask) -> FleetTaskResult:
        start = time.monotonic()
        self._emit(
            "fleet.task.start",
            task_index=task_index,
            persona=task.persona,
            goal=task.goal[:120],
        )

        token = None
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

            repo = find_repo_config(workspace)
            task_config = merge_repo_into_fleet_config(self.config, repo)
            resolver = YamlPersonaResolver(task_config)

            phases = self._resolve_pipeline(task)
            if phases == ["full"]:
                result = self._run_full_pipeline(task_index, task, workspace, start)
                self._emit(
                    "fleet.task.complete",
                    task_index=task_index,
                    persona=task.persona,
                    status=result.status,
                    duration_seconds=result.duration_seconds,
                )
                return result

            phase_results, summary, exit_code = run_pipeline(
                backend=self.backend,
                resolver=resolver,
                task=task,
                workspace=workspace,
                timeout_s=task_config.timeout_seconds,
                phases=phases,
            )
            agent_id = None
            for phase in reversed(phase_results):
                if phase.get("agent_id"):
                    agent_id = phase["agent_id"]
                    break

            status = "completed" if exit_code == 0 else "error"
            error = None
            if exit_code != 0:
                last = phase_results[-1] if phase_results else {}
                error = last.get("stderr") or last.get("error") or "Cursor agent failed"

            result = FleetTaskResult(
                task_index=task_index,
                persona=task.persona,
                goal=task.goal,
                status=status,
                summary=summary or None,
                error=error,
                duration_seconds=round(time.monotonic() - start, 2),
                agent_id=agent_id,
                phases={p.get("phase", "?"): p for p in phase_results},
            )
            self._emit(
                "fleet.task.complete",
                task_index=task_index,
                persona=task.persona,
                status=status,
                duration_seconds=result.duration_seconds,
            )
            return result
        except Exception as exc:
            logger.exception("Fleet task %s failed", task_index)
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
        tasks: list[dict[str, Any]] | None = None,
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
                f"Too many tasks ({len(normalized)}). "
                f"max_parallel={self.config.max_parallel}"
            )

        if len(normalized) == 1:
            return [self._run_one(0, normalized[0])]

        results: list[FleetTaskResult | None] = [None] * len(normalized)
        with ThreadPoolExecutor(max_workers=len(normalized)) as pool:
            futures = {
                pool.submit(self._run_one, idx, task): idx
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
    tasks: list[dict[str, Any]] | None = None,
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
