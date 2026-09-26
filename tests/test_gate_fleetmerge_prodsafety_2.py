"""An exclusive group whose peer has no work must not deadlock the busy repo.

``alpha`` and ``beta`` share an exclusive group with ``post_merge_hold_seconds:
0``, and turn taking is driven by the group's persisted ``last_served``. The
intent of the strict-alternation check is that whichever repo deployed last
yields so a peer with a single ready batch cannot be starved by a repo with
constant work. But when the peer has no ready batch at all it never runs, so
``last_served`` never advances off ``alpha`` and the alternation condition can
never be satisfied again. A repo with steady approved work would then be held
on every future tick, forever, with nothing ever merging.

Three consecutive real ticks, one eligible batch for ``alpha`` each time: the
first merges, and the second and third must still merge. The ledger must not
record ``alpha`` as having gone last while no peer had a turn to take.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.merge_plan.execute import run_tick
from agent_fleet.merge_plan.types import (
    ApprovedPR,
    Batch,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)


class StubClient:
    """A GitHubClient stand-in whose PR state is a plain dict."""

    def __init__(self, prs: dict[int, dict[str, Any]]) -> None:
        self._prs = prs

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        return self._prs.get(pr_number, {})

    def for_repo(self, repo_path: Path | None) -> StubClient:
        del repo_path
        return self


def open_pr(sha: str) -> dict[str, Any]:
    return {"state": "OPEN", "headRefOid": sha, "mergeable": "MERGEABLE"}


def recorder(record: Path, *, tag: str = "merge") -> str:
    """A command that logs that it ran, then exits 0."""
    runner = record.parent / f"runner-{tag}.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(
        "import json, sys\n"
        f"record, tag = {str(record)!r}, {tag!r}\n"
        "with open(record, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'tag': tag, 'argv': sys.argv[1:]}) + '\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {runner}"


def ran(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]


def test_steady_repo_is_not_held_forever_when_exclusive_peer_is_idle(
    tmp_path: Path,
) -> None:
    """Alpha has work every tick and beta has none: alpha must keep merging."""
    state_dir = tmp_path / "merge-state"
    state_dir.mkdir()
    record = tmp_path / "ran.jsonl"

    alpha, beta = "alpha", "beta"
    spec = ExecutorSpec(
        state_dir=str(state_dir), exclusive_groups=((alpha, beta),), post_merge_hold_seconds=0
    )
    repo_specs = {
        alpha: RepoSpec(name=alpha, merge_template=recorder(record)),
        beta: RepoSpec(name=beta, merge_template=recorder(record)),
    }
    client = StubClient({1: open_pr("aaaa111")})

    statuses: list[str] = []
    details: list[str] = []
    for _ in range(3):
        # Each tick plans exactly one eligible batch, for alpha only: beta is
        # idle in production, so it never appears in a plan at all.
        batch = Batch(
            index=0,
            repo=alpha,
            prs=(ApprovedPR(repo=alpha, pr_number=1, approved_sha="aaaa111", head_sha="aaaa111"),),
            deploy_unit="alpha-api",
            executor_commands=(),
        )
        result = run_tick(
            plan=MergePlan(batches=(batch,)),
            spec=spec,
            repo_specs=repo_specs,
            client=client,
        )
        statuses.append(result.outcomes[0].status)
        details.append(result.outcomes[0].detail)

    # The merge command really ran on every tick: this is a behaviour failure,
    # not an environment or import failure.
    assert statuses[0] == "merged", f"first tick should merge: {statuses} {details}"
    assert statuses == ["merged", "merged", "merged"], (
        f"a repo with steady work was held forever by an idle exclusive peer: "
        f"statuses={statuses} details={details} merge-runs={len(ran(record))}"
    )
    assert len(ran(record)) == 3, "each tick must really run the merge command"
