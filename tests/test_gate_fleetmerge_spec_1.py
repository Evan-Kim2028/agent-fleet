"""An idle exclusive-group peer must not starve a repo that has a queue.

lake-of-rage shares an exclusive group with silphcoanalytics. silphcoanalytics
is idle -- it contributes no batch to the plan -- so it can never take its
turn. Turn-taking must therefore not hold lake-of-rage's *remaining* batches
behind an absent peer, or the approved queue is stranded forever, which is the
"stalled queue" the runner exists to eliminate.
"""

from __future__ import annotations

import sys
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from agent_fleet.merge_plan.execute import run_tick
from agent_fleet.merge_plan.types import (
    ApprovedPR,
    Batch,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)

LOR = "lake-of-rage"
PEER = "silphcoanalytics"


class StubClient:
    """Minimal PRReader: PR state comes from a dict, never from the network."""

    def __init__(self, prs: dict[int, dict[str, Any]]) -> None:
        self._prs = prs

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        return self._prs.get(pr_number, {})

    def for_repo(self, repo_path: Path | None) -> StubClient:
        del repo_path
        return self


def open_pr(sha: str) -> dict[str, Any]:
    return {"state": "OPEN", "headRefOid": sha, "mergeable": "MERGEABLE"}


def recorder(record: Path) -> str:
    """A real merge command that logs its invocation, so 'it ran' is observed."""
    runner = record.parent / "runner.py"
    runner.write_text(
        "import json, sys\n"
        f"record = {str(record)!r}\n"
        "with open(record, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {runner}"


def pr(number: int, sha: str) -> ApprovedPR:
    return ApprovedPR(repo=LOR, pr_number=number, approved_sha=sha, head_sha=sha, lane="")


def batch(index: int, prs: Sequence[ApprovedPR]) -> Batch:
    return Batch(index=index, repo=LOR, prs=tuple(prs), deploy_unit="lor-api", executor_commands=())


def test_idle_exclusive_peer_does_not_starve_a_repo_with_a_queue(tmp_path: Path) -> None:
    """Three lake-of-rage batches, no silphcoanalytics batch: all three ship.

    A group alternates turns, but a turn can only be handed to a peer that is
    actually in the plan. With the peer idle, the first batch merges and then
    holds its own queue forever.
    """
    record = tmp_path / "ran.jsonl"
    spec = ExecutorSpec(
        state_dir=str(tmp_path / "state"),
        exclusive_groups=((LOR, PEER),),
        post_merge_hold_seconds=0,
    )
    repos = {
        LOR: RepoSpec(name=LOR, path="", merge_template=recorder(record)),
        PEER: RepoSpec(name=PEER, path="", merge_template=""),
    }

    shas = ["aaaa111", "bbbb222", "cccc333", "dddd444", "eeee555", "ffff666", "9999aaa"]
    client = StubClient({i + 1: open_pr(s) for i, s in enumerate(shas)})
    batches = [
        batch(0, [pr(1, shas[0]), pr(2, shas[1]), pr(3, shas[2])]),
        batch(1, [pr(4, shas[3]), pr(5, shas[4]), pr(6, shas[5])]),
        batch(2, [pr(7, shas[6])]),
    ]

    clock = {"t": 1000.0}

    def tick() -> list[tuple[int, str, str]]:
        result = run_tick(
            plan=MergePlan(batches=tuple(batches)),
            spec=spec,
            repo_specs=repos,
            client=client,
            now=lambda: clock["t"],
            run_id="t",
        )
        return [(o.index, o.status, o.detail) for o in result.outcomes]

    ticks = [tick() for _ in range(4)]

    merged = {i for tick_outcomes in ticks for i, status, _ in tick_outcomes if status == "merged"}
    assert merged == {0, 1, 2}, (
        f"approved PRs never ship -- the idle exclusive-group peer starved the repo: {ticks}"
    )
    ran_lines = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    assert len(ran_lines) == 3, f"merge command ran {len(ran_lines)} times, expected 3"


def test_second_batch_is_held_by_the_alternation_rule(tmp_path: Path) -> None:
    """Pin the exact mechanism: the first tick holds the repo's own next batch.

    Reported as a separate observation so the failure above names its cause.
    """
    record = tmp_path / "ran.jsonl"
    spec = ExecutorSpec(
        state_dir=str(tmp_path / "state"),
        exclusive_groups=((LOR, PEER),),
        post_merge_hold_seconds=0,
    )
    repos = {
        LOR: RepoSpec(name=LOR, path="", merge_template=recorder(record)),
        PEER: RepoSpec(name=PEER, path="", merge_template=""),
    }
    shas = ["aaaa111", "bbbb222", "cccc333", "dddd444", "eeee555", "ffff666", "9999aaa"]
    client = StubClient({i + 1: open_pr(s) for i, s in enumerate(shas)})
    batches = [
        batch(0, [pr(1, shas[0]), pr(2, shas[1]), pr(3, shas[2])]),
        batch(1, [pr(4, shas[3]), pr(5, shas[4]), pr(6, shas[5])]),
        batch(2, [pr(7, shas[6])]),
    ]
    now: Callable[[], float] = lambda: 1000.0
    result = run_tick(
        plan=MergePlan(batches=tuple(batches)),
        spec=spec,
        repo_specs=repos,
        client=client,
        now=now,
        run_id="t",
    )
    statuses = {o.index: (o.status, o.detail) for o in result.outcomes}
    assert statuses[0][0] == "merged", statuses
    assert statuses[1][0] == "merged", (
        "batch 1 was held in the same tick it could have shipped: "
        f"the turn-taking rule fired against an idle peer -- {statuses[1]}"
    )
