"""Run a repo's ``plan_command`` and cache its result per PR head sha.

The cache matters because a merged batch is not one PR: three PRs may all touch
``gold_sales``, and the planner is a dbt/Dagster query that is far too slow to
run once per PR. Keying on the head sha means a re-run of the same batch is
free, and a PR whose head moved plans again.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.post_merge.types import MergedPR, Plan, parse_plan

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_fleet.post_merge.config import RepoSpec

#: How a planner's stdin and stdout are produced. Tests substitute this; the
#: default spawns the real command.
RunFn = Callable[["Sequence[str]", str, str, int], "subprocess.CompletedProcess[str]"]


def _run(
    argv: Sequence[str], stdin: str, cwd: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run *argv* without a shell, feeding *stdin* to it.

    No shell, deliberately: a config template can quote its arguments but can
    never be re-interpreted, and the planner is handed a list of file paths that
    came from the forge.
    """
    return subprocess.run(
        list(argv),
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd or None,
        check=False,
        timeout=timeout,
    )


def plan_path(cache_dir: Path, head_sha: str) -> Path:
    """Where the plan for *head_sha* is cached."""
    return cache_dir / f"{head_sha}.json"


def cached_plan(cache_dir: Path, head_sha: str) -> Plan | None:
    """The cached plan for *head_sha*, or ``None`` when absent or unreadable.

    An unreadable cache entry is a miss, not an error: the planner is the source
    of truth and re-running it is always safe.
    """
    path = plan_path(cache_dir, head_sha)
    if not head_sha or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return Plan.from_dict(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class PlanResult:
    """A PR, its plan, and whether the plan came from the cache.

    The PR travels with the plan because the hand-off note needs the title and
    merge commit next to the tables, and re-reading them from the forge at note
    time would be a second round trip for data we already had.
    """

    pr: MergedPR
    plan: Plan
    #: True when this came out of the cache rather than the planner.
    cached: bool = False

    @property
    def pr_number(self) -> int:
        return self.pr.number

    @property
    def head_sha(self) -> str:
        return self.pr.head_sha


def repo_path(spec: RepoSpec) -> str:
    """The repo's checkout, with ``~`` expanded, or ``""`` when unset.

    Every other path in this package is expanded, and an unexpanded ``~`` is not
    a directory: passing it through hands subprocess a literal ``~`` and raises
    FileNotFoundError.
    """
    return str(Path(spec.path).expanduser()) if spec.path else ""


def plan_argv(spec: RepoSpec) -> list[str]:
    """``plan_command`` split into argv, with its program resolved against the repo.

    A command naming a path (``scripts/plan.sh``) is documented as relative to
    the checkout, so it is anchored there rather than left to the fleet
    process's cwd — the same rule the trigger runner applies.
    """
    argv = shlex.split(spec.plan_command)
    if not argv:
        return argv
    program = argv[0]
    if "/" in program:
        argv[0] = str(Path(repo_path(spec)) / program)
    return argv


def plan_for_pr(
    spec: RepoSpec,
    pr: MergedPR,
    *,
    run: RunFn = _run,
) -> PlanResult:
    """The plan for *pr*, from the cache when the head sha already has one."""
    cache_dir = spec.cache_dir()
    # No head sha means no cache key and nothing to plan. An empty Plan() here
    # would be indistinguishable from a real "nothing to rebuild", so it would
    # label the PR rebuild:none and queue no jobs while looking successful.
    if not pr.head_sha:
        raise RuntimeError(f"PR #{pr.number} has no head sha, so it cannot be planned")

    hit = cached_plan(cache_dir, pr.head_sha)
    if hit is not None:
        return PlanResult(pr, hit, cached=True)

    result = run(
        plan_argv(spec),
        "\n".join(pr.files) + ("\n" if pr.files else ""),
        repo_path(spec),
        spec.plan_timeout_seconds,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-400:]
        raise RuntimeError(
            f"plan_command for PR #{pr.number} failed (rc={result.returncode}): {detail}"
        )
    plan = parse_plan(result.stdout or "")
    _write_cache(cache_dir, pr.head_sha, plan)
    return PlanResult(pr, plan, cached=False)


def _write_cache(cache_dir: Path, head_sha: str, plan: Plan) -> None:
    """Persist *plan* under its head sha, best-effort.

    A cache that cannot be written must not fail the merge: the plan is already
    in hand and re-planning next run is merely slower.
    """
    path = plan_path(cache_dir, head_sha)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
    except OSError:
        return


def dedupe_jobs(results: list[PlanResult]) -> list[tuple[str, str]]:
    """Unique ``(job, slot)`` pairs across a whole batch, in first-seen order.

    This is the batch-level dedupe: three PRs naming the same job produce one
    run, and the first PR to name it also decides its slot.
    """
    seen: dict[str, str] = {}
    for result in results:
        for job in result.plan.jobs:
            seen.setdefault(job.job, job.slot)
    return list(seen.items())
