"""``agent-fleet merge-plan`` — decide which approved PRs ship together.

Merging and deploying is the slowest serialized step in the fleet's loop
(~15-20 min for a lake-of-rage deploy, ~9 min for silphcoanalytics), so the
command center's job is to pick batches that share one deploy instead of
shipping approved PRs one at a time.

See ``docs/MERGE-PLAN.md`` for the algorithm and the fleet.yaml config.
"""

from __future__ import annotations

from agent_fleet.merge_plan.batching import plan_batches
from agent_fleet.merge_plan.config import load_merge_plan_config, resolve_repo_specs
from agent_fleet.merge_plan.plan import build_plan, emit_plan_event, render_plan_text
from agent_fleet.merge_plan.types import (
    DEFAULT_MAX_BATCH_SIZE,
    ApprovedPR,
    Batch,
    ChangeProfile,
    MergePlan,
    RepoSpec,
)

__all__ = [
    "DEFAULT_MAX_BATCH_SIZE",
    "ApprovedPR",
    "Batch",
    "ChangeProfile",
    "MergePlan",
    "RepoSpec",
    "build_plan",
    "emit_plan_event",
    "load_merge_plan_config",
    "plan_batches",
    "render_plan_text",
    "resolve_repo_specs",
]
