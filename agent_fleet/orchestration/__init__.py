"""Task decomposition and child dispatch orchestration."""

from agent_fleet.orchestration.config import OrchestrationConfig, resolve_orchestration_config
from agent_fleet.orchestration.decompose import (
    aggregate_child_results,
    child_tasks_from_task_spec,
    coerce_empty_decompose,
    dispatch_task_spec_children,
    enrich_task_from_task_spec,
    handle_preflight_decision,
    preflight_plan,
)
from agent_fleet.orchestration.equip import resolve_dispatch_equip

__all__ = [
    "OrchestrationConfig",
    "aggregate_child_results",
    "child_tasks_from_task_spec",
    "dispatch_task_spec_children",
    "enrich_task_from_task_spec",
    "handle_preflight_decision",
    "preflight_plan",
    "resolve_dispatch_equip",
    "resolve_orchestration_config",
]
