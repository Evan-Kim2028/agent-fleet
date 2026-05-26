"""Repo-defined workstream batches — first-class multi-task fleet dispatch."""

from agent_fleet.workstreams.config import (
    WorkstreamItem,
    WorkstreamsConfig,
    load_workstreams_config,
)
from agent_fleet.workstreams.dispatch import run_workstreams
from agent_fleet.workstreams.harvest import harvest_worktree
from agent_fleet.workstreams.scope import find_scope_overlaps, validate_parallel_batch

__all__ = [
    "WorkstreamItem",
    "WorkstreamsConfig",
    "find_scope_overlaps",
    "harvest_worktree",
    "load_workstreams_config",
    "run_workstreams",
    "validate_parallel_batch",
]
