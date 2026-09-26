"""Assemble a MergePlan and (optionally) emit it for the dashboard."""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

from agent_fleet.merge_plan.batching import plan_batches
from agent_fleet.merge_plan.collect import (
    GitHubClient,
    collect_from_lanes,
    collect_from_status_dir,
    dedupe_approvals,
    profile_approvals,
)
from agent_fleet.merge_plan.types import DEFAULT_MAX_BATCH_SIZE, MergePlan

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agent_fleet.merge_plan.types import ApprovedPR, ChangeProfile, RepoSpec

logger = logging.getLogger(__name__)


def build_plan(
    *,
    repo_specs: dict[str, RepoSpec],
    operator: str | None = None,
    status_dir: Path | None = None,
    lanes_root: Path | None = None,
    client: GitHubClient | None = None,
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    check_merges: bool = True,
) -> MergePlan:
    """Collect approved PRs, profile them, and batch them.

    The caller passes a *client* so tests can inject a fake ``gh``; the
    default constructs a real one per repo path.
    """
    approvals: list[ApprovedPR] = list(collect_from_lanes(operator=operator, lanes_root=lanes_root))
    if status_dir is not None:
        approvals.extend(collect_from_status_dir(status_dir))
    # Normalise the repo spelling *before* de-duplicating: the same PR is
    # recorded as ``owner/name#N`` by one source and as ``name`` by another, and
    # de-duping on the raw strings would keep both, planning one PR twice and
    # handing the operator's merge script the same PR twice in one batch.
    approvals = dedupe_approvals([_normalize_repo(pr, repo_specs) for pr in approvals])
    approvals = [p for p in approvals if p.repo in repo_specs]

    notes: list[str] = []
    if not approvals:
        notes.append("no PREMERGE-APPROVED status lines found for the selected repos/operators")

    # One client, re-scoped per repo inside profile_approvals: gh resolves a
    # bare PR number against the checkout it runs in.
    client = client or GitHubClient()
    batchable, profiles, stale, unprofilable = profile_approvals(
        approvals, client=client, repo_specs=repo_specs
    )
    excluded: list[ChangeProfile] = [*stale, *unprofilable]
    if stale:
        notes.append(f"{len(stale)} stale approval(s) excluded — head moved since the gate ran")

    batches = plan_batches(
        batchable,
        profiles,
        repo_specs=repo_specs,
        max_batch_size=max_batch_size,
        check_merges=check_merges,
    )
    unconfigured = sorted({b.repo for b in batches if not b.executor_commands})
    if unconfigured:
        notes.append(
            "no executor template configured for: "
            + ", ".join(unconfigured)
            + " (set merge_plan.repos[].merge_template in fleet.yaml)"
        )
    return MergePlan(
        batches=tuple(batches),
        excluded=tuple(excluded),
        notes=tuple(notes),
        max_batch_size=max_batch_size,
    )


def _normalize_repo(pr: ApprovedPR, repo_specs: dict[str, RepoSpec]) -> ApprovedPR:
    """Rewrite an approval's repo to the key used in *repo_specs*.

    Gate and lane sources record a PR's repo as ``owner/name`` while a
    ``--repo-path`` yields the bare ``name``, so the two have to be reconciled
    or every approval looks like it belongs to an unselected repo.
    """
    if pr.repo in repo_specs:
        return pr
    short = pr.repo.rsplit("/", 1)[-1]
    for name in repo_specs:
        if name.rsplit("/", 1)[-1] == short:
            if name == pr.repo:
                return pr
            return replace(pr, repo=name)
    return pr


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plan_text(plan: MergePlan) -> str:
    """Human-readable plan, shaped for an operator reading a terminal."""
    lines: list[str] = []
    if not plan.batches:
        lines.append("merge-plan: no mergeable approved PRs")
    for batch in plan.batches:
        prs = ", ".join(f"#{p.pr_number}@{p.sha9}" for p in batch.prs)
        header = f"batch {batch.index}: {batch.repo} [{batch.deploy_unit or 'no-unit'}] {prs}"
        lines.append(header)
        if batch.dbt_select:
            lines.append(f"  dbt --select {' '.join(batch.dbt_select)}")
        if batch.executor_commands:
            for command in batch.executor_commands:
                lines.append(f"  $ {command}")
        else:
            lines.append("  (no executor template configured for this repo)")
        for reason in batch.reasons:
            lines.append(f"  - {reason}")
    for profile in plan.excluded:
        lines.append(f"excluded #{profile.pr_number} {profile.repo}: {profile.reason}")
    for note in plan.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Emission (feature-detected)
# ---------------------------------------------------------------------------


def _fleetobs_emit_available() -> bool:
    """True when the parallel fb/fleetobs lane provides an ``emit`` command.

    Probed by import first (cheap, no subprocess) and then by ``--help`` on
    the CLI entry point, so a half-landed parallel lane degrades to the
    JSONL fallback instead of failing the run.
    """
    if importlib.util.find_spec("agent_fleet.observability.emit_cli") is not None:
        return True
    binary = shutil.which("agent-fleet")
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "emit", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "usage" in (result.stdout or "").lower()


def emit_plan_event(plan: MergePlan, *, run_id: str) -> str:
    """Emit the plan as a ``merge.plan`` event; returns the sink used.

    Prefers the ``fb/fleetobs`` ``emit`` command when it is present, otherwise
    appends a FleetEvent-shaped line under the runs dir so ``dash`` can still
    pick it up.
    """
    payload = plan.to_dict()
    if _fleetobs_emit_available():
        binary = shutil.which("agent-fleet") or "agent-fleet"
        argv = [binary, "emit", "--event", "merge.plan", "--json", json.dumps(payload)]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode == 0:
            return "fleetobs:emit"
        logger.warning("fleetobs emit failed, falling back to JSONL: %s", result.stderr.strip())

    from agent_fleet.fleet_paths import default_runs_dir
    from agent_fleet.observability.events import FleetEvent

    event = FleetEvent.now(run_id=run_id, event="merge.plan", data=payload)
    path = default_runs_dir() / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(event.to_json() + "\n")
    return f"jsonl:{path}"


def plan_event_summary(plan: MergePlan) -> Sequence[str]:
    """One-line-per-batch summary for the dashboard."""
    return [
        f"{b.index}:{b.repo}:{b.deploy_unit or '-'}:" + ",".join(str(p.pr_number) for p in b.prs)
        for b in plan.batches
    ]
