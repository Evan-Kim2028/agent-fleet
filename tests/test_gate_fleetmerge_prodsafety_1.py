"""``--dry-run`` must not write turn-taking state to the hold ledger.

The executor already refuses to run commands and refuses to take a deploy
lock on a dry run, because a preview must not block the real process that is
about to do the work.  ``ledger.record_served`` is the third way a preview
can act on the world: it is the durable half of the cross-repo fairness
bookkeeping, and it is reached on the same code path that a real merge
reaches.  A dry run that records "alpha just served" has quietly consumed
alpha's turn in the ``alpha|beta`` exclusive group and can hold the peer
repo for the whole post-merge window, for a merge that never happened.

The pre-existing dry-run test does not catch this: it runs with no
``exclusive_groups``, so ``spec.group_for(repo)`` is empty and
``record_served`` is never called at all.

No network, no real repository: PR state comes from a stub client and the
merge command is a real subprocess that would write to a temp file if it ran.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from agent_fleet.merge_plan.execute import HoldLedger, run_tick
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


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "merge-state"
    d.mkdir()
    return d


@pytest.fixture
def record(tmp_path: Path) -> Path:
    """Where a stub merge command appends one JSON line per invocation."""
    return tmp_path / "ran.jsonl"


def recorder(record: Path, *, tag: str = "merge") -> str:
    runner = record.parent / f"runner-{tag}.py"
    runner.write_text(
        "import json, sys\n"
        f"record, tag = {str(record)!r}, {tag!r}\n"
        "with open(record, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'tag': tag, 'argv': sys.argv[1:]}) + '\\n')\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {runner}"


def ran(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]


def spec_for(repo: str, **fields: str) -> RepoSpec:
    spec = RepoSpec(name=repo)
    for key, value in fields.items():
        setattr(spec, key, value)
    return spec


def pr(number: int, sha: str, *, repo: str) -> ApprovedPR:
    return ApprovedPR(repo=repo, pr_number=number, approved_sha=sha, head_sha=sha, lane="")


def batch_for(repo: str, prs: Sequence[ApprovedPR], *, index: int = 0) -> Batch:
    return Batch(
        index=index, repo=repo, prs=tuple(prs), deploy_unit=f"{repo}-api", executor_commands=()
    )


def tick(
    *,
    batches: Sequence[Batch],
    spec: ExecutorSpec,
    repo_specs: dict[str, RepoSpec],
    client: StubClient,
    dry_run: bool = False,
    now: Callable[[], float] = time.time,
) -> Any:
    return run_tick(
        plan=MergePlan(batches=tuple(batches)),
        spec=spec,
        repo_specs=repo_specs,
        client=client,
        dry_run=dry_run,
        run_id="gate-prodsafety-1",
        now=now,
    )


def test_dry_run_does_not_write_served_state_to_the_ledger(state_dir: Path, record: Path) -> None:
    """A preview must not consume a turn in the exclusive group.

    Alpha is fully eligible, shares an exclusive group with beta, and the
    group is configured with a real post-merge hold.  Every command the tick
    would run is suppressed, yet the tick still completes the batch and calls
    ``ledger.record_served`` on the way out.
    """
    alpha, beta = "lake-of-rage", "silphcoanalytics"
    ledger = HoldLedger(state_dir / "ledger.json")
    clock = {"t": 1000.0}
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        exclusive_groups=((alpha, beta),),
        post_merge_hold_seconds=300,
    )
    repo_specs = {
        alpha: spec_for(alpha, merge_template=recorder(record)),
        beta: spec_for(beta, merge_template=recorder(record)),
    }
    client = StubClient({1: {"state": "OPEN", "headRefOid": "aaaa111", "mergeable": "MERGEABLE"}})

    result = tick(
        batches=[batch_for(alpha, [pr(1, "aaaa111", repo=alpha)])],
        spec=spec,
        repo_specs=repo_specs,
        client=client,
        dry_run=True,
        now=lambda: clock["t"],
    )

    # Sanity: this is the dry run the executor already claims to be inert.
    assert result.by_status("merged"), "the batch is eligible; nothing should have held it"
    assert ran(record) == [], "dry run executed a command"
    assert not (state_dir / f"deploy-{alpha}.lock").exists(), "dry run took a lock"

    # The defect: the durable fairness record is written anyway.
    assert not (state_dir / "ledger.json").exists(), (
        "dry run wrote the ledger; a read-only preview must leave no trace"
    )
    assert ledger.last_served((alpha, beta)) == "", (
        "dry run recorded alpha as the last repo served in its exclusive group"
    )
    assert ledger.hold_until((alpha, beta)) == 0.0, (
        "dry run armed a post-merge hold for a merge that never happened"
    )


def test_dry_run_does_not_hold_out_its_peer_repo(state_dir: Path, record: Path) -> None:
    """The user-visible consequence: beta is locked out of a preview that merged nothing.

    Without the dry run writing the ledger, the same plan merges alpha
    immediately and yields beta to the *next* tick on fairness grounds.  With
    the write, the phantom record holds the whole group for the configured
    window even though the real command never ran.
    """
    alpha, beta = "lake-of-rage", "silphcoanalytics"
    clock = {"t": 1000.0}
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        exclusive_groups=((alpha, beta),),
        post_merge_hold_seconds=300,
    )
    repo_specs = {
        alpha: spec_for(alpha, merge_template=recorder(record)),
        beta: spec_for(beta, merge_template=recorder(record)),
    }
    client = StubClient({1: {"state": "OPEN", "headRefOid": "aaaa111", "mergeable": "MERGEABLE"}})

    tick(
        batches=[batch_for(alpha, [pr(1, "aaaa111", repo=alpha)])],
        spec=spec,
        repo_specs=repo_specs,
        client=client,
        dry_run=True,
        now=lambda: clock["t"],
    )

    # One second later, on a different repo, in a brand new tick: beta has real
    # work and is not a peer of a merge that happened. It must not be held.
    client2 = StubClient({2: {"state": "OPEN", "headRefOid": "bbbb222", "mergeable": "MERGEABLE"}})
    after = tick(
        batches=[batch_for(beta, [pr(2, "bbbb222", repo=beta)])],
        spec=spec,
        repo_specs=repo_specs,
        client=client2,
        now=lambda: clock["t"] + 1.0,
    )

    assert after.outcomes[0].status == "merged", (
        f"a dry run for alpha held out beta: {after.outcomes[0].detail}"
    )
    assert [c["tag"] for c in ran(record)] == ["merge"], "beta's merge command did not run"
