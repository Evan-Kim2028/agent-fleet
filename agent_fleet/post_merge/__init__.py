"""``agent-fleet post-merge`` — what happens after code merges.

Merging a PR that touches dbt models does not rebuild the lake. Something has
to read the diff, decide which tables moved, rebuild them, and tell the data
side to go and validate. Today that is a bash script per repo; this package
makes it a per-repo configured feature so the same four steps — plan, label,
trigger, hand off — run identically everywhere and are testable without a lake.

The repo supplies the two domain-specific pieces (a ``plan_command`` that knows
its dbt/Dagster graph, and a ``trigger_command`` that knows its slots); the
batching, caching, label diffing, job dedupe, and note rendering live here.

See ``docs/POST_MERGE.md`` for the config and the hand-off contract.
"""

from __future__ import annotations

from agent_fleet.post_merge.config import RepoSpec, load_repo_specs, resolve_repo_spec
from agent_fleet.post_merge.flow import PostMergeResult, run_post_merge
from agent_fleet.post_merge.handoff import Batch, index_line, render_note, write_note
from agent_fleet.post_merge.labels import apply_labels, diff_for, gh_labeler
from agent_fleet.post_merge.planner import (
    PlanResult,
    cached_plan,
    dedupe_jobs,
    plan_for_pr,
)
from agent_fleet.post_merge.trigger import JobOutcome, read_ledger, run_jobs
from agent_fleet.post_merge.types import (
    Job,
    LabelDelta,
    MergedPR,
    Plan,
    label_diff,
    parse_plan,
)

__all__ = [
    "Batch",
    "Job",
    "JobOutcome",
    "LabelDelta",
    "MergedPR",
    "Plan",
    "PlanResult",
    "PostMergeResult",
    "RepoSpec",
    "apply_labels",
    "cached_plan",
    "dedupe_jobs",
    "diff_for",
    "gh_labeler",
    "index_line",
    "label_diff",
    "load_repo_specs",
    "parse_plan",
    "plan_for_pr",
    "read_ledger",
    "render_note",
    "resolve_repo_spec",
    "run_jobs",
    "run_post_merge",
    "write_note",
]
