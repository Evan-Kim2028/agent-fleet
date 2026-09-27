"""The post-merge flow: plan → label → trigger → hand off.

One function, four steps, in that order:

1. **plan** every PR through the repo's ``plan_command``, cached per head sha;
2. **label** each PR to exactly match its plan, removing stale older labels;
3. **trigger** each job once, batch-deduped and ledger-deduped, only when the
   deploy returned 0;
4. **hand off** a markdown note plus one INDEX line for the downstream data
   agent.

Every external effect — forge, planner, trigger, filesystem — is injectable, so
the whole flow is unit-testable with no network and no subprocess.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.post_merge.config import expand_path
from agent_fleet.post_merge.handoff import Batch, write_note
from agent_fleet.post_merge.labels import apply_labels, gh_labeler
from agent_fleet.post_merge.planner import dedupe_jobs, plan_for_pr
from agent_fleet.post_merge.trigger import run_jobs
from agent_fleet.post_merge.types import MergedPR

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_fleet.post_merge.config import RepoSpec
    from agent_fleet.post_merge.labels import ApplyFn
    from agent_fleet.post_merge.planner import PlanResult, RunFn
    from agent_fleet.post_merge.trigger import JobOutcome, TriggerFn


@dataclass
class PostMergeResult:
    """Everything one ``fleet post-merge`` run did, for reporting."""

    repo: str
    prs: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    plans: list[PlanResult] = field(default_factory=list)
    label_changes: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = field(default_factory=dict)
    jobs: list[JobOutcome] = field(default_factory=list)
    note_path: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing

    @property
    def cached_plans(self) -> int:
        return sum(1 for p in self.plans if p.cached)

    def to_dict(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "prs": list(self.prs),
            "missing": list(self.missing),
            "plans": [p.plan.to_dict() for p in self.plans],
            "cached_plans": self.cached_plans,
            "label_changes": {
                str(pr): {"add": list(add), "remove": list(remove)}
                for pr, (add, remove) in self.label_changes.items()
            },
            "jobs": [j.to_dict() for j in self.jobs],
            "note": str(self.note_path) if self.note_path else "",
            "errors": list(self.errors),
            "ok": self.ok,
        }



#: Everything ``gh pr view`` is asked for, in one place. ``gh`` returns only the
#: fields requested, so a key missing from the payload is a real absence.
PR_FIELDS = "number,title,headRefOid,mergeCommit,mergedAt,files,labels"


def _fetch_via_gh(pr: int, repo_path: str) -> MergedPR | None:
    """Read one PR's metadata through the ``gh`` CLI."""
    import json

    result = subprocess.run(
        ["gh", "pr", "view", str(pr), "--json", PR_FIELDS],
        capture_output=True,
        text=True,
        cwd=repo_path or None,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    try:
        raw = json.loads(result.stdout or "")
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    data = dict(raw)
    # gh nests the merge commit and returns labels as objects; flatten both.
    commit = data.get("mergeCommit")
    if isinstance(commit, dict):
        data["mergeCommit"] = str(commit.get("oid") or "")
    labels = data.get("labels")
    if isinstance(labels, list):
        data["labels"] = [
            str(i.get("name", "")) if isinstance(i, dict) else str(i) for i in labels
        ]
    return MergedPR.from_dict(data, number=pr)


def _fetch_prs(
    numbers: Sequence[int],
    repo_path: str,
    fetch: FetchFn | None,
) -> tuple[list[MergedPR], list[int]]:
    """Fetch each PR, returning the ones found and the numbers that were not.

    An unreadable PR is reported rather than dropped: a missing PR must not
    silently shrink the batch, or a rebuild gets queued for an incomplete set.
    """
    getter = fetch or _fetch_via_gh
    found: list[MergedPR] = []
    missing: list[int] = []
    for number in numbers:
        pr = getter(number, repo_path)
        if pr is None:
            missing.append(number)
        else:
            found.append(pr)
    return found, missing

def run_post_merge(
    spec: RepoSpec,
    prs: Sequence[int],
    *,
    deploy_rc: int,
    main_sha: str = "",
    repo_path: str = "",
    fetch: FetchFn | None = None,
    run_plan: RunFn | None = None,
    apply: ApplyFn | None = None,
    trigger: TriggerFn | None = None,
    write_notes: bool = True,
) -> PostMergeResult:
    """Run the full post-merge flow for one repo's merged batch.

    A planner that fails on one PR does not abort the batch: that PR is recorded
    as an error and the rest still get labelled, triggered, and handed off, so
    a single bad plan cannot strand the other merges.
    """
    result = PostMergeResult(repo=spec.name, prs=list(prs))

    # fetch=None means "use the gh-backed default", not "everything is missing".
    # The checkout is expanded once here and every step below reuses it: `~` is
    # not a directory, so the raw form dies in the first subprocess.
    workdir = expand_path(repo_path or spec.path)
    fetched, missing = _fetch_prs(prs, workdir, fetch)
    result.missing = missing

    # 1. Plan, per PR, tolerating a single planner failure.
    for pr in fetched:
        try:
            kwargs = {"run": run_plan} if run_plan is not None else {}
            result.plans.append(plan_for_pr(spec, pr, **kwargs))
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            # TimeoutExpired is a SubprocessError, not an OSError, so a planner
            # that blows plan_timeout_seconds used to abort the whole batch
            # before the hand-off note. One bad plan is one PR's error.
            result.errors.append(f"PR #{pr.number}: {exc}")
    result.errors.extend(f"PR #{n}: could not read from the forge" for n in missing)

    # 2. Label each PR to exactly its plan. apply=None means the real gh
    #    labeler — skipping labelling by default would be the silent bug where
    #    a merged PR is planned and rebuilt but never carries its labels.
    labeler = apply if apply is not None else gh_labeler(workdir)
    for planned in result.plans:
        try:
            delta = apply_labels(
                planned.pr_number,
                current=planned.pr.labels,
                desired=planned.plan.labels(),
                apply=labeler,
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            # TimeoutExpired is a SubprocessError, not an OSError: a gh label
            # call that blows its 120s timeout used to escape run_post_merge
            # entirely, so the trigger never fired, no note was written and no
            # INDEX line recorded the outstanding rebuild — the operator got a
            # traceback instead of the hand-off. Losing one label must not cost
            # the whole batch.
            result.errors.append(f"PR #{planned.pr_number}: labelling failed: {exc}")
            continue
        result.label_changes[planned.pr_number] = (delta.add, delta.remove)

    # 3. Trigger each job once across the whole batch.
    result.jobs = run_jobs(
        spec,
        dedupe_jobs(result.plans),
        deploy_rc=deploy_rc,
        trigger=trigger,
        batch=main_sha,
    )

    # 4. Hand off one note per batch.
    if write_notes and spec.handoff_inbox:
        batch = Batch(
            repo=spec.name,
            main_sha=main_sha,
            deploy_rc=deploy_rc,
            results=result.plans,
            outcomes=result.jobs,
        )
        result.note_path = write_note(batch, Path(spec.handoff_inbox).expanduser())

    return result


#: Called with (pr_number, repo_path) to fetch one PR. Tests substitute this.
FetchFn = Callable[[int, str], "MergedPR | None"]
