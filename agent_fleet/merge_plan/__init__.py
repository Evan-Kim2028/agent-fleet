"""``agent-fleet merge-plan`` and ``agent-fleet merge`` — plan, then ship.

Merging and deploying is the slowest serialized step in the fleet's loop
(~15-20 min for a lake-of-rage deploy, ~9 min for silphcoanalytics), so the
command center's job is to pick batches that share one deploy instead of
shipping approved PRs one at a time.

:mod:`agent_fleet.merge_plan.execute` is the other half: it runs the batches
this package plans, under a process-held deploy lock, with cluster holds and
fair cross-repo alternation.

See ``docs/MERGE-PLAN.md`` for the algorithm and the fleet.yaml config.
"""

from __future__ import annotations

from agent_fleet.merge_plan.batching import plan_batches
from agent_fleet.merge_plan.config import (
    load_executor_spec,
    load_merge_plan_config,
    parse_executor_spec,
    resolve_repo_specs,
)
from agent_fleet.merge_plan.execute import (
    BatchOutcome,
    CommandResult,
    DeployLock,
    HoldLedger,
    TickResult,
    deploy_lock_for,
    release_hold,
    run_daemon,
    run_tick,
)
from agent_fleet.merge_plan.plan import build_plan, emit_plan_event, render_plan_text
from agent_fleet.merge_plan.types import (
    DEFAULT_MAX_BATCH_SIZE,
    ApprovedPR,
    Batch,
    ChangeProfile,
    ClusterHold,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)

__all__ = [
    "DEFAULT_MAX_BATCH_SIZE",
    "ApprovedPR",
    "Batch",
    "BatchOutcome",
    "ChangeProfile",
    "ClusterHold",
    "CommandResult",
    "DeployLock",
    "ExecutorSpec",
    "HoldLedger",
    "MergePlan",
    "RepoSpec",
    "TickResult",
    "build_plan",
    "deploy_lock_for",
    "emit_plan_event",
    "load_executor_spec",
    "load_merge_plan_config",
    "parse_executor_spec",
    "plan_batches",
    "release_hold",
    "render_plan_text",
    "resolve_repo_specs",
    "run_daemon",
    "run_tick",
]
