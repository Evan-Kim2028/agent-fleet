"""A repo's own second batch in a tick is held by the alternation rule.

``lake-of-rage`` sits in an exclusive group with ``silphcoanalytics``.  When a
tick's plan carries three batches for ``lake-of-rage`` and none for its peer --
the normal shape once a repo has more approved PRs than ``max_batch_size`` --
the first batch merges, ``served.record`` writes ``lake-of-rage`` as the group's
``last_served``, and the turn-taking check in ``_process_batch`` then matches
that repo against its own turn for every remaining batch in the tick.

The strict alternation exists so a repo with constant work cannot starve a peer
that has one batch ready.  With no peer batch in the plan there is nobody to
starve, so the remaining batches must ship: the group is not shared this tick,
the post-merge hold has lapsed, and the repo's deploy slot is free.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from agent_fleet.merge_plan.execute import TickResult, run_tick
from agent_fleet.merge_plan.types import ApprovedPR, Batch, ExecutorSpec, MergePlan, RepoSpec


class StubClient:
    """A GitHubClient stand-in: the executor only reads ``pr_detail``."""

    def __init__(self, prs: dict[int, dict[str, Any]]) -> None:
        self._prs = prs

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        return self._prs.get(pr_number, {})

    def for_repo(self, repo_path: Path | None) -> StubClient:
        del repo_path
        return self


def _recorder(record: Path) -> str:
    """A real command that appends one JSON line, so "it ran" is observed."""
    runner = record.parent / "runner.py"
    runner.write_text(
        "import json, sys\n"
        f"record = {str(record)!r}\n"
        "with open(record, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {runner}"


def test_every_batch_of_a_single_repo_ships_in_one_tick(tmp_path: Path) -> None:
    lake, silph = "lake-of-rage", "silphcoanalytics"
    record = tmp_path / "ran.jsonl"
    state_dir = tmp_path / "merge-state"
    state_dir.mkdir()

    spec = ExecutorSpec(
        state_dir=str(state_dir), exclusive_groups=((lake, silph),), post_merge_hold_seconds=0
    )
    repo_specs = {
        lake: RepoSpec(name=lake, merge_template=_recorder(record)),
        silph: RepoSpec(name=silph, merge_template=_recorder(record)),
    }
    shas = {1: "aaaa1111", 2: "bbbb2222", 3: "cccc3333"}
    client = StubClient(
        {n: {"state": "OPEN", "headRefOid": s, "mergeable": "MERGEABLE"} for n, s in shas.items()}
    )
    batches = [
        Batch(
            index=i,
            repo=lake,
            prs=(
                ApprovedPR(
                    repo=lake,
                    pr_number=n,
                    approved_sha=shas[n],
                    head_sha=shas[n],
                    lane=f"work-{n}",
                ),
            ),
            deploy_unit="lor-api",
        )
        for i, n in enumerate((1, 2, 3))
    ]

    result: TickResult = run_tick(
        plan=MergePlan(batches=tuple(batches)),
        spec=spec,
        repo_specs=repo_specs,
        client=client,
        now=time.time,
    )

    statuses = [o.status for o in result.outcomes]
    assert statuses == ["merged", "merged", "merged"], [o.to_dict() for o in result.outcomes]
    assert {o.pr_numbers for o in result.outcomes} == {(1,), (2,), (3,)}
    ran = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]
    assert len(ran) == 3, f"merge command ran {len(ran)} times, expected 3"
