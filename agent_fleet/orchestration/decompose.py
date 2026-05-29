"""Auto-decompose: fan out TaskSpec children and aggregate results."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from agent_fleet.contracts.task_spec import DecompositionDecision, TaskSpec
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.level_up.models import DispatchEquip
from agent_fleet.orchestration.convergence import PARTIAL_OK, SUCCESS_STATUSES, compact_summary
from agent_fleet.orchestration.primitives import DispatchPrimitives
from agent_fleet.planner import plan

if TYPE_CHECKING:
    from agent_fleet.dispatcher import FleetDispatcher
    from agent_fleet.hooks import LLMBackend, LLMSession, PersonaResolver
    from agent_fleet.persona_foundry import PersonaFoundry
    from agent_fleet.spine_config import SpineConfig

logger = logging.getLogger(__name__)


def coerce_empty_decompose(task_spec: TaskSpec) -> tuple[TaskSpec, bool]:
    """Downgrade decompose-with-no-children to single so the run can proceed."""
    if (
        task_spec.decomposition_decision != DecompositionDecision.DECOMPOSE
        or task_spec.child_issues_proposed
    ):
        return task_spec, False
    note = (
        "[orchestration fallback] decompose with no child_issues_proposed; "
        "continuing as single-agent run."
    )
    reason = task_spec.decomposition_reason
    merged_reason = f"{reason} {note}".strip() if reason else note
    logger.warning(
        "Planner chose decompose for issue #%s but proposed no children; falling back to single",
        task_spec.issue_number,
    )
    return (
        replace(
            task_spec,
            decomposition_decision=DecompositionDecision.SINGLE,
            decomposition_reason=merged_reason,
        ),
        True,
    )


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
        equip=task.equip,
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
    parent_run_id: str | None = None,
    foundry: PersonaFoundry | None = None,
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
            if foundry is not None:
                persona = foundry.resolve_or_generate(persona, known, fallback_persona)
            else:
                logger.warning(
                    "Child task %r persona %r not in fleet; using %r",
                    title,
                    persona,
                    fallback_persona,
                )
                persona = fallback_persona
        goal = title if body == title else f"{title}\n\n{body}"
        context = _child_context(child, task_spec, parent_context=parent_context)
        child_equip = (
            DispatchEquip(
                persona=persona,
                base_loadout=persona,
                skill_slots_execute=(),
                skill_slots_review=(),
                level_up_generation=0,
                parent_run_id=parent_run_id,
            )
            if parent_run_id
            else None
        )
        children.append(
            FleetTask(
                goal=goal,
                context=context,
                persona=persona,
                workspace=workspace,
                pipeline=child_pipeline,
                title=title,
                equip=child_equip,
            )
        )
    return children


def aggregate_child_results(results: list[FleetTaskResult]) -> tuple[str, str | None, str]:
    """Return (aggregate_status, error, summary)."""
    if not results:
        return "decompose_failed", "No child tasks were dispatched", ""

    summary = compact_summary(results)

    if all(r.status in SUCCESS_STATUSES | PARTIAL_OK for r in results):
        return "completed", None, summary
    if any(r.status in SUCCESS_STATUSES | PARTIAL_OK for r in results):
        failed = [r for r in results if r.status not in SUCCESS_STATUSES | PARTIAL_OK]
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
    parent_run_id: str | None = None,
    foundry: PersonaFoundry | None = None,
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
        parent_run_id=parent_run_id,
        foundry=foundry,
    )

    limit = max_parallel or dispatcher.config.max_parallel
    if len(children) > limit:
        logger.info(
            "Dispatching %d children in waves (max_parallel=%d)",
            len(children),
            limit,
        )

    primitives = DispatchPrimitives(dispatcher, max_parallel=limit)
    all_results = primitives.run_many(children)

    status, error, summary = aggregate_child_results(all_results)
    return all_results, status, error, summary


def handle_preflight_decision(
    task_spec: TaskSpec,
) -> tuple[str, str | None]:
    """Map planner decision to dispatcher status when not auto-dispatching."""
    if task_spec.decomposition_decision == DecompositionDecision.REJECTED:
        return "rejected", task_spec.decomposition_reason
    if task_spec.decomposition_decision == DecompositionDecision.DAG:
        if not task_spec.dag:
            return "single", None
        return "dag", task_spec.decomposition_reason or "Task requires DAG dispatch"
    if task_spec.decomposition_decision == DecompositionDecision.DECOMPOSE:
        if not task_spec.child_issues_proposed:
            return "single", None
        return (
            "decompose",
            task_spec.decomposition_reason or "Task requires decomposition",
        )
    return "single", None
