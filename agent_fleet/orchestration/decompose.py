"""Auto-decompose: fan out TaskSpec children and aggregate results."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.task_spec import DecompositionDecision, TaskSpec
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.planner import plan

if TYPE_CHECKING:
    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.hooks import LLMBackend, LLMSession, PersonaResolver
    from agent_fleet.spine_config import SpineConfig

logger = logging.getLogger(__name__)

_SUCCESS_STATUSES = frozenset({"completed", "merged"})
_PARTIAL_OK = frozenset({"review_changes_requested"})


def preflight_plan(
    *,
    task: FleetTask,
    task_id: int,
    backend: LLMBackend,
    persona_resolver: PersonaResolver,
    spine_config: SpineConfig | None = None,
    session: LLMSession | None = None,
) -> TaskSpec:
    """Run PLAN-only for a FleetTask before code_review execute."""
    title = task.title or task.goal[:120]
    body = task.goal.strip()
    if task.context.strip():
        body = f"{body}\n\n## Context\n{task.context.strip()}"
    return plan(
        task_id,
        title,
        body,
        backend=backend,
        persona_resolver=persona_resolver,
        spine_config=spine_config,
        session=session,
    )


def enrich_task_from_task_spec(task: FleetTask, task_spec: TaskSpec) -> FleetTask:
    """Inject planner acceptance criteria and scope into a single-agent task."""
    parts: list[str] = []
    if task.context.strip():
        parts.append(task.context.strip())
    if task_spec.acceptance_criteria:
        parts.append("## Acceptance criteria")
        parts.extend(f"- {item}" for item in task_spec.acceptance_criteria)
    if task_spec.scope.allowed_paths:
        parts.append("## Planned scope (path prefixes)")
        parts.append(", ".join(task_spec.scope.allowed_paths))
    if task_spec.scope.forbidden_paths:
        parts.append("## Forbidden paths")
        parts.append(", ".join(task_spec.scope.forbidden_paths))
    merged = "\n\n".join(parts)
    return FleetTask(
        goal=task.goal,
        context=merged,
        persona=task.persona,
        workspace=task.workspace,
        pipeline=task.pipeline,
        title=task.title,
    )


def _child_context(
    child: dict[str, Any],
    task_spec: TaskSpec,
    *,
    parent_context: str = "",
) -> str:
    parts: list[str] = []
    child_body = str(child.get("body") or "").strip()
    if child_body:
        parts.append(child_body)
    if task_spec.acceptance_criteria:
        parts.append("## Parent acceptance criteria")
        parts.extend(f"- {item}" for item in task_spec.acceptance_criteria)
    coord = task_spec.coordination_spec or {}
    brief = coord.get("interface_brief")
    if brief:
        parts.append("## Interface contract (code against this; siblings share it)")
        parts.append(json.dumps(brief, indent=2, sort_keys=True))
    merge_order = coord.get("merge_order")
    if isinstance(merge_order, list) and merge_order:
        parts.append("## Suggested merge order")
        parts.extend(f"- {item}" for item in merge_order)
    allowed = child.get("allowed_paths")
    if isinstance(allowed, list) and allowed:
        parts.append("## Scope — only modify")
        parts.append(", ".join(str(p) for p in allowed))
    if parent_context.strip():
        parts.append("## Parent context")
        parts.append(parent_context.strip())
    if task_spec.decomposition_reason:
        parts.append("## Decomposition rationale")
        parts.append(task_spec.decomposition_reason)
    return "\n\n".join(parts)


def child_tasks_from_task_spec(
    task_spec: TaskSpec,
    *,
    parent_task: FleetTask,
    child_pipeline: str,
    persona_resolver: PersonaResolver,
    fallback_persona: str,
) -> list[FleetTask]:
    """Build FleetTask list from planner child_issues_proposed."""
    known = set(persona_resolver.list_personas())
    workspace = parent_task.workspace
    parent_context = parent_task.context
    children: list[FleetTask] = []
    for index, child in enumerate(task_spec.child_issues_proposed, start=1):
        title = str(child.get("title") or f"Child {index}").strip()
        body = str(child.get("body") or title).strip()
        persona = str(child.get("persona") or fallback_persona)
        if persona not in known:
            logger.warning(
                "Child task %r persona %r not in fleet; using %r",
                title,
                persona,
                fallback_persona,
            )
            persona = fallback_persona
        goal = title if body == title else f"{title}\n\n{body}"
        context = _child_context(child, task_spec, parent_context=parent_context)
        children.append(
            FleetTask(
                goal=goal,
                context=context,
                persona=persona,
                workspace=workspace,
                pipeline=child_pipeline,
                title=title,
            )
        )
    return children


def aggregate_child_results(results: list[FleetTaskResult]) -> tuple[str, str | None, str]:
    """Return (aggregate_status, error, summary)."""
    if not results:
        return "decompose_failed", "No child tasks were dispatched", ""

    successes = sum(1 for r in results if r.status in _SUCCESS_STATUSES)
    partial = sum(1 for r in results if r.status in _PARTIAL_OK)
    failures = len(results) - successes - partial

    lines = [
        f"Decomposed into {len(results)} child task(s): "
        f"{successes} completed, {partial} review pending, {failures} failed."
    ]
    for result in results:
        lines.append(f"- [{result.persona}] {result.goal[:80]} → {result.status}")
        if result.summary:
            lines.append(f"  {result.summary[:200]}")

    summary = "\n".join(lines)

    if successes + partial == len(results):
        return "completed", None, summary
    if successes > 0 or partial > 0:
        failed = [r for r in results if r.status not in _SUCCESS_STATUSES | _PARTIAL_OK]
        err = "; ".join(f"{r.persona}: {r.error or r.status}" for r in failed[:3])
        return "decompose_partial", err or "Some child tasks failed", summary
    err = results[0].error or results[0].status
    return "decompose_failed", err, summary


def dispatch_task_spec_children(
    *,
    task_spec: TaskSpec,
    parent_task: FleetTask,
    dispatcher: FleetDispatcher,
    child_pipeline: str,
    persona_resolver: PersonaResolver,
    fallback_persona: str,
    max_parallel: int | None = None,
) -> tuple[list[FleetTaskResult], str, str | None, str]:
    """Fan out child tasks through the fleet dispatcher.

    Returns (child_results, aggregate_status, error, summary).
    """
    if task_spec.decomposition_decision != DecompositionDecision.DECOMPOSE:
        raise ValueError(f"Expected decompose decision, got {task_spec.decomposition_decision!r}")
    if not task_spec.child_issues_proposed:
        return (
            [],
            "decompose_failed",
            "Planner chose decompose but proposed no child tasks",
            task_spec.decomposition_reason,
        )

    children = child_tasks_from_task_spec(
        task_spec,
        parent_task=parent_task,
        child_pipeline=child_pipeline,
        persona_resolver=persona_resolver,
        fallback_persona=fallback_persona,
    )

    limit = max_parallel or dispatcher.config.max_parallel
    if len(children) > limit:
        logger.info(
            "Dispatching %d children in waves (max_parallel=%d)",
            len(children),
            limit,
        )

    all_results: list[FleetTaskResult] = []
    for offset in range(0, len(children), limit):
        wave = children[offset : offset + limit]
        wave_results = dispatcher.dispatch(
            tasks=[
                {
                    "goal": t.goal,
                    "context": t.context,
                    "persona": t.persona,
                    "workspace": t.workspace,
                    "pipeline": t.pipeline,
                }
                for t in wave
            ],
        )
        all_results.extend(wave_results)

    status, error, summary = aggregate_child_results(all_results)
    return all_results, status, error, summary


def handle_preflight_decision(
    task_spec: TaskSpec,
) -> tuple[str, str | None]:
    """Map planner decision to dispatcher status when not auto-dispatching."""
    if task_spec.decomposition_decision == DecompositionDecision.REJECTED:
        return "rejected", task_spec.decomposition_reason
    if task_spec.decomposition_decision == DecompositionDecision.DECOMPOSE:
        return (
            "decompose",
            task_spec.decomposition_reason or "Task requires decomposition",
        )
    return "single", None
