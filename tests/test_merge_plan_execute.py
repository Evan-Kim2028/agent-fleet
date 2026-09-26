"""Tests for ``agent_fleet.merge_plan.execute`` — the merge runner.

One test per failure the ad-hoc merge scripts actually hit, so each is
impossible to reintroduce without a red test:

1. a lock leaked by a merge that exited early, deadlocking the next repo
2. a conflicting PR at the head of a queue blocking every other approval
3. a hand-maintained "already merged" list stranding twelve approved PRs
4. a head that moved after approval (must never merge)
5. two repos deploying at once, and the starvation a naive wait causes
6. a cluster hold with no way to release it

Nothing here touches the network or a real repository. PR state comes from a
stub client; commands are real subprocesses that write to a temp file, so
"the merge command ran" is an observation rather than a mock assertion.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from agent_fleet.merge_plan.config import parse_executor_spec
from agent_fleet.merge_plan.execute import (
    DeployLock,
    HoldLedger,
    TickResult,
    command_argv,
    deploy_lock_for,
    run_tick,
)
from agent_fleet.merge_plan.types import (
    ApprovedPR,
    Batch,
    ClusterHold,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class StubClient:
    """A GitHubClient stand-in whose PR state is a plain dict.

    Only ``pr_detail`` is needed by the executor, so the tests declare state
    (state/mergeable/headRefOid) directly instead of shimming a ``gh`` binary.
    """

    def __init__(self, prs: dict[int, dict[str, Any]]) -> None:
        self._prs = prs
        self.calls: list[int] = []

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        self.calls.append(pr_number)
        return self._prs.get(pr_number, {})

    def for_repo(self, repo_path: Path | None) -> StubClient:
        # A stand-in needs no per-repo scoping: its table is keyed by PR number
        # and these tests never read two repositories through one client.
        del repo_path
        return self


def open_pr(sha: str, *, mergeable: str = "MERGEABLE") -> dict[str, Any]:
    return {"state": "OPEN", "headRefOid": sha, "mergeable": mergeable}


def merged_pr(sha: str) -> dict[str, Any]:
    return {"state": "MERGED", "headRefOid": sha, "mergeable": "UNKNOWN"}


def conflicting_pr(sha: str) -> dict[str, Any]:
    return {"state": "OPEN", "headRefOid": sha, "mergeable": "CONFLICTING"}


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "merge-state"
    d.mkdir()
    return d


@pytest.fixture
def exec_spec(state_dir: Path) -> ExecutorSpec:
    """Inert settings pointed at a temp state dir: no holds, no exclusivity."""
    return ExecutorSpec(state_dir=str(state_dir))


@pytest.fixture
def record(tmp_path: Path) -> Path:
    """Where stub merge commands append one JSON line per invocation."""
    return tmp_path / "ran.jsonl"


def recorder(record: Path, *, exit_code: int = 0, tag: str = "merge") -> str:
    """A command that logs that it ran, then exits *exit_code*.

    This is a real subprocess writing a real file, so "the merge command ran"
    is an observation rather than a mock assertion, and a quoting mistake in
    template rendering shows up as a non-zero exit instead of passing silently.
    """
    runner = record.parent / f"runner-{tag}.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(
        "import json, sys\n"
        f"record, tag, code = {str(record)!r}, {tag!r}, {exit_code!r}\n"
        "with open(record, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'tag': tag, 'argv': sys.argv[1:]}) + '\\n')\n"
        "sys.exit(code)\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {runner}"


def ran(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]


def spec_for(repo: str, *, path: str = "", **fields: str) -> RepoSpec:
    spec = RepoSpec(name=repo, path=path)
    for key, value in fields.items():
        setattr(spec, key, value)
    return spec


def tick(
    *,
    batches: Sequence[Batch],
    spec: ExecutorSpec,
    repo_specs: dict[str, RepoSpec],
    client: StubClient,
    dry_run: bool = False,
    run_id: str = "",
    now: Callable[[], float] = time.time,
) -> TickResult:
    return run_tick(
        plan=MergePlan(batches=tuple(batches)),
        spec=spec,
        repo_specs=repo_specs,
        client=client,
        dry_run=dry_run,
        run_id=run_id,
        now=now,
    )


def one_batch(
    prs: Sequence[ApprovedPR], *, index: int = 0, repo: str = "lake-of-rage", unit: str = "lor-api"
) -> Batch:
    return Batch(index=index, repo=repo, prs=tuple(prs), deploy_unit=unit, executor_commands=())


def pr(number: int, sha: str, *, repo: str = "lake-of-rage", lane: str = "") -> ApprovedPR:
    return ApprovedPR(repo=repo, pr_number=number, approved_sha=sha, head_sha=sha, lane=lane)


# ---------------------------------------------------------------------------
# 1. A lock that cannot outlive its process
# ---------------------------------------------------------------------------


def test_lock_released_when_merge_command_raises(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original deadlock: a script exiting on a conflict kept its lock.

    Here the merge command raises instead of exiting, which is the harder case
    -- an exception unwinds past every ordinary cleanup -- and the lock must
    still be free afterwards.
    """
    import agent_fleet.merge_plan.execute as execute_mod

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("merge command blew up")

    monkeypatch.setattr(execute_mod, "_run_command", boom)

    repo = "lake-of-rage"
    with pytest.raises(RuntimeError):
        tick(
            batches=[one_batch([pr(1, "aaaa111")])],
            spec=ExecutorSpec(state_dir=str(state_dir)),
            repo_specs={repo: spec_for(repo, merge_template="anything")},
            client=StubClient({1: open_pr("aaaa111")}),
        )

    # The second repo's merge must not wait on the first repo's dead lock.
    lock = deploy_lock_for(state_dir, repo)
    assert lock.try_acquire(), "lock leaked past an exception; the next repo would deadlock"
    lock.release()


def test_lock_released_when_merge_command_fails_cleanly(state_dir: Path, record: Path) -> None:
    """A non-zero exit (the common conflict case) must also release the lock."""
    repo = "lake-of-rage"
    repo_specs = {repo: spec_for(repo, merge_template=recorder(record, exit_code=3))}
    batch = one_batch([pr(1, "aaaa111")])
    client = StubClient({1: open_pr("aaaa111")})

    result = tick(
        batches=[batch],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=client,
    )
    assert result.outcomes[0].status == "needs_rebase"
    assert deploy_lock_for(state_dir, repo).try_acquire()
    deploy_lock_for(state_dir, repo).release()


def test_live_lock_blocks_second_acquire(state_dir: Path) -> None:
    """A lock held by a running process is never stolen, even with stale meta."""
    lock = deploy_lock_for(state_dir, "lake-of-rage")
    assert lock.try_acquire()
    other = DeployLock(state_dir / "deploy-lake-of-rage.lock", repo="lake-of-rage")
    assert not other.try_acquire(), "overlapping deploys are exactly what the lock prevents"
    lock.release()
    assert other.try_acquire()
    other.release()


def test_stale_lock_whose_holder_is_gone_is_reclaimed(state_dir: Path) -> None:
    """A leftover .meta naming a dead pid must not pin a lock forever.

    The kernel already dropped the flock when that process died, so the
    reclaim is really about the record: it must not report a phantom holder.
    """
    lock = deploy_lock_for(state_dir, "lake-of-rage")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("", encoding="utf-8")
    lock.meta_path.write_text(
        json.dumps({"pid": 999_999_999, "boot_id": "x", "repo": "lake-of-rage"}),
        encoding="utf-8",
    )
    assert not DeployLock(lock.path, repo="lake-of-rage").holder_is_alive()
    assert lock.try_acquire()
    lock.release()


def test_stale_lock_from_a_previous_boot_is_reclaimed(state_dir: Path) -> None:
    """A lock recorded before a reboot is stale even if the pid is reused."""
    lock = deploy_lock_for(state_dir, "lake-of-rage")
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("", encoding="utf-8")
    lock.meta_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),  # genuinely alive: only the boot id differs
                "boot_id": "00000000-0000-0000-0000-000000000000",
                "repo": "lake-of-rage",
            }
        ),
        encoding="utf-8",
    )
    assert not DeployLock(lock.path, repo="lake-of-rage").holder_is_alive()
    assert lock.try_acquire()
    lock.release()


def test_holder_is_alive_for_this_process(state_dir: Path) -> None:
    """The happy path: a lock this very process holds is reported live."""
    lock = deploy_lock_for(state_dir, "lake-of-rage")
    lock.try_acquire()
    assert DeployLock(lock.path, repo="lake-of-rage").holder_is_alive()
    lock.release()


def test_lock_is_not_stolen_when_repo_is_busy(state_dir: Path, record: Path) -> None:
    """A batch whose repo another process is deploying is reported, not forced."""
    repo = "lake-of-rage"
    lock = deploy_lock_for(state_dir, repo)
    assert lock.try_acquire()  # simulate a concurrent fleet merge
    try:
        result = tick(
            batches=[one_batch([pr(1, "aaaa111")])],
            spec=ExecutorSpec(state_dir=str(state_dir)),
            repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
            client=StubClient({1: open_pr("aaaa111")}),
        )
    finally:
        lock.release()
    assert result.outcomes[0].status == "locked"
    assert ran(record) == [], "the merge ran despite the repo being locked"


# ---------------------------------------------------------------------------
# 2. A conflicting PR must not block the rest of the queue
# ---------------------------------------------------------------------------


def test_conflicting_pr_is_removed_and_rebase_command_runs(
    state_dir: Path, record: Path, tmp_path: Path
) -> None:
    """PR 1 conflicts, PRs 2 and 3 are fine: 2 and 3 must still merge."""
    repo = "lake-of-rage"
    merge_record = record
    rebase_record = tmp_path / "rebase.jsonl"
    repo_specs = {
        repo: spec_for(
            repo,
            merge_template=recorder(merge_record),
            rebase_template=recorder(rebase_record, tag="rebase"),
        )
    }
    batch = one_batch([pr(1, "aaaa111"), pr(2, "bbbb222"), pr(3, "cccc333")])
    client = StubClient(
        {1: conflicting_pr("aaaa111"), 2: open_pr("bbbb222"), 3: open_pr("cccc333")}
    )

    result = tick(
        batches=[batch],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=client,
    )

    outcome = result.outcomes[0]
    assert outcome.status == "merged", "head-of-line blocking is the failure we are preventing"
    assert outcome.prs == (2, 3), "the conflicting PR must be out of the batch"
    assert outcome.needs_rebase == (1,)
    assert [c["tag"] for c in ran(merge_record)] == ["merge"]
    assert [c["tag"] for c in ran(rebase_record)] == ["rebase"]
    assert "merge.needs_rebase" in result.events


def test_conflicting_pr_does_not_block_another_repo(state_dir: Path, record: Path) -> None:
    """Repo A's conflict must not stop repo B from shipping."""
    a, b = "lake-of-rage", "silphcoanalytics"
    a_record, b_record = record, record.parent / "b.jsonl"
    repo_specs = {
        a: spec_for(a, merge_template=recorder(a_record, exit_code=3)),
        b: spec_for(b, merge_template=recorder(b_record)),
    }
    client = StubClient(
        {1: conflicting_pr("aaaa111"), 2: open_pr("bbbb222")},
    )
    result = tick(
        batches=[
            one_batch([pr(1, "aaaa111")], index=0, repo=a),
            one_batch([pr(2, "bbbb222")], index=1, repo=b),
        ],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=client,
    )
    statuses = {o.repo: o.status for o in result.outcomes}
    assert statuses[a] == "needs_rebase"
    assert statuses[b] == "merged", "repo B was blocked by repo A's conflict"
    assert len(ran(b_record)) == 1


def test_rebase_failure_is_reported_not_retried(
    state_dir: Path, record: Path, tmp_path: Path
) -> None:
    """A rebase that fails is reported once; the tick does not spin on it."""
    repo = "lake-of-rage"
    rebase_record = tmp_path / "rebase.jsonl"
    repo_specs = {
        repo: spec_for(
            repo,
            merge_template=recorder(record),
            rebase_template=recorder(rebase_record, exit_code=1, tag="rebase"),
        )
    }
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=StubClient({1: conflicting_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "needs_rebase"
    assert len(ran(rebase_record)) == 1, "rebase ran more than once for one conflict"


def test_needs_rebase_without_a_configured_rebase_command(state_dir: Path, record: Path) -> None:
    """No rebase command is not a crash; the PR is still held out of the batch."""
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: conflicting_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "needs_rebase"
    assert result.outcomes[0].needs_rebase == (1,)
    assert ran(record) == []


# ---------------------------------------------------------------------------
# 3. Merge state comes from GitHub, not from a list
# ---------------------------------------------------------------------------


def test_already_merged_pr_is_skipped_from_live_state(state_dir: Path, record: Path) -> None:
    """A PR GitHub reports MERGED is skipped because GitHub says so.

    This is the regression test for the hand-seeded hold list that stranded
    twelve approved PRs.  Nothing is remembered between ticks: the skip is
    derived from the PR's state at the moment the tick runs.
    """
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: merged_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "skipped"
    assert "already merged" in result.outcomes[0].detail
    assert ran(record) == []


def test_no_merged_ledger_is_written(state_dir: Path, record: Path) -> None:
    """The ledger must never accumulate merge bookkeeping.

    It holds only operator intent and fairness counters, so it cannot drift
    out of sync with reality the way the old hold file did.
    """
    repo = "lake-of-rage"
    tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: merged_pr("aaaa111")}),
    )
    ledger = HoldLedger(state_dir / "ledger.json")._read()
    assert "merged" not in ledger
    assert "prs" not in ledger


def test_in_flight_lane_does_not_block_other_prs(state_dir: Path, record: Path) -> None:
    """A lane that is mid-flight is simply not MERGED yet, so it still runs.

    The old hold list was seeded with in-flight lanes, which is precisely how
    approved work stopped shipping.  Nothing seeds a list here.
    """
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111"), pr(2, "bbbb222")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111"), 2: open_pr("bbbb222")}),
    )
    assert result.outcomes[0].status == "merged"
    assert len(ran(record)) == 1


# ---------------------------------------------------------------------------
# 4. A moved head must never merge
# ---------------------------------------------------------------------------


def test_head_moved_between_plan_and_merge_is_refused(state_dir: Path, record: Path) -> None:
    """Approved at X, head at Y by merge time: the merge command must not run."""
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("ffff999")}),  # head moved off aaaa111
    )
    assert result.outcomes[0].status == "skipped"
    assert "head moved" in result.outcomes[0].detail
    assert ran(record) == []


def test_unreadable_pr_state_is_not_treated_as_safe(state_dir: Path, record: Path) -> None:
    """An unreadable PR is skipped, never merged on the assumption it is fine."""
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({}),  # gh returned nothing
    )
    assert result.outcomes[0].status == "skipped"
    assert ran(record) == []


def test_mergeability_unknown_is_skipped(state_dir: Path, record: Path) -> None:
    """GitHub's transient UNKNOWN must not be read as permission to merge."""
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111", mergeable="UNKNOWN")}),
    )
    assert result.outcomes[0].status == "skipped"
    assert ran(record) == []


# ---------------------------------------------------------------------------
# 5. Cross-repo exclusivity, with fairness
# ---------------------------------------------------------------------------


def test_exclusive_repos_alternate(state_dir: Path, record: Path) -> None:
    """A and B share a group: they take turns, and never run in the same tick."""
    a, b = "lake-of-rage", "silphcoanalytics"
    spec = ExecutorSpec(
        state_dir=str(state_dir), exclusive_groups=((a, b),), post_merge_hold_seconds=0
    )
    repo_specs = {
        a: spec_for(a, merge_template=recorder(record)),
        b: spec_for(b, merge_template=recorder(record)),
    }
    client = StubClient({1: open_pr("aaaa111"), 2: open_pr("bbbb222")})
    batches = [
        one_batch([pr(1, "aaaa111")], index=0, repo=a),
        one_batch([pr(2, "bbbb222")], index=1, repo=b),
    ]
    order = []
    for _ in range(6):
        result = tick(batches=batches, spec=spec, repo_specs=repo_specs, client=client)
        order.append([o.repo for o in result.outcomes if o.status == "merged"])
    # Never two overlapping deploys in one tick.
    assert all(len(repos) <= 1 for repos in order), order
    served = [repos[0] for repos in order if repos]
    # Alternation is real: over six ticks both repos get several turns, rather
    # than one side winning every time (starvation) or neither ever running.
    assert served == [a, b, a, b, a, b], f"no fair alternation: {served}"


def test_exclusive_repo_without_work_does_not_starve_the_other(
    state_dir: Path, record: Path
) -> None:
    """B has work while A is idle: B must run, not wait for A's turn."""
    a, b = "lake-of-rage", "silphcoanalytics"
    spec = ExecutorSpec(
        state_dir=str(state_dir), exclusive_groups=((a, b),), post_merge_hold_seconds=0
    )
    repo_specs = {
        a: spec_for(a, merge_template=recorder(record)),
        b: spec_for(b, merge_template=recorder(record)),
    }
    client = StubClient({2: open_pr("bbbb222")})
    # Only B is in the plan; A being idle must not make B yield.
    result = tick(
        batches=[one_batch([pr(2, "bbbb222")], index=0, repo=b)],
        spec=spec,
        repo_specs=repo_specs,
        client=client,
    )
    assert result.outcomes[0].status == "merged", "an idle peer must not starve a ready repo"


def test_post_merge_hold_delays_the_group(state_dir: Path, record: Path) -> None:
    """After a merge the group is quiet for the configured hold, then reopens."""
    a, b = "lake-of-rage", "silphcoanalytics"
    clock = {"t": 1000.0}
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        exclusive_groups=((a, b),),
        post_merge_hold_seconds=300,
    )
    repo_specs = {
        a: spec_for(a, merge_template=recorder(record)),
        b: spec_for(b, merge_template=recorder(record)),
    }
    client = StubClient({1: open_pr("aaaa111"), 2: open_pr("bbbb222")})
    batches = [
        one_batch([pr(1, "aaaa111")], index=0, repo=a),
        one_batch([pr(2, "bbbb222")], index=1, repo=b),
    ]

    def run_at(t: float) -> TickResult:
        clock["t"] = t
        return tick(
            batches=batches, spec=spec, repo_specs=repo_specs, client=client, now=lambda: clock["t"]
        )

    # A deploys, then the group goes quiet for 300s.
    first = run_at(1000.0)
    assert [o.status for o in first.outcomes] == ["merged", "held"]
    assert "post-merge hold" in first.outcomes[1].detail

    # Well inside the hold window, nobody deploys.
    assert all(o.status == "held" for o in run_at(1010.0).outcomes)

    # Once it lapses, the peer gets its turn -- the hold is a delay, not a
    # deadlock, and the group does not stay stuck on whichever repo went first.
    after = run_at(1400.0)
    assert after.by_status("merged"), [o.to_dict() for o in after.outcomes]
    assert after.by_status("merged")[0].repo == b, "the peer's turn never came around"


# ---------------------------------------------------------------------------
# 6. Cluster holds
# ---------------------------------------------------------------------------


def test_active_hold_blocks_matching_lane(state_dir: Path, record: Path) -> None:
    """A held lane does not merge, and the reason names the hold."""
    repo = "lake-of-rage"
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        holds=(ClusterHold(name="pass2-downstream", lanes=("sales-pass2-*",)),),
    )
    result = tick(
        batches=[one_batch([pr(1, "aaaa111", lane="sales-pass2-1")])],
        spec=spec,
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "held"
    assert "pass2-downstream" in result.outcomes[0].detail
    assert ran(record) == []


def test_hold_by_deploy_unit(state_dir: Path, record: Path) -> None:
    """A hold can be expressed on the deploy unit rather than the lane."""
    repo = "lake-of-rage"
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        holds=(ClusterHold(name="dbt-gate", deploy_units=("lor-api",)),),
    )
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")], unit="lor-api")],
        spec=spec,
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "held"
    assert "dbt-gate" in result.outcomes[0].detail


def test_unheld_lane_is_unaffected_by_someone_elses_hold(state_dir: Path, record: Path) -> None:
    """A hold is scoped: a different lane merges normally."""
    repo = "lake-of-rage"
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        holds=(ClusterHold(name="pass2-downstream", lanes=("sales-pass2-*",)),),
    )
    result = tick(
        batches=[one_batch([pr(1, "aaaa111", lane="unrelated-lane")])],
        spec=spec,
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "merged"


def test_release_clears_the_hold(state_dir: Path, record: Path) -> None:
    """After `fleet merge release`, the held lane merges again."""
    repo = "lake-of-rage"
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        holds=(ClusterHold(name="pass2-downstream", lanes=("sales-pass2-*",)),),
    )
    repo_specs = {repo: spec_for(repo, merge_template=recorder(record))}
    client = StubClient({1: open_pr("aaaa111")})
    batches = [one_batch([pr(1, "aaaa111", lane="sales-pass2-1")])]

    assert (
        tick(batches=batches, spec=spec, repo_specs=repo_specs, client=client).outcomes[0].status
        == "held"
    )

    ledger = HoldLedger(state_dir / "ledger.json")
    assert ledger.release("pass2-downstream") is True
    assert ledger.release("pass2-downstream") is False, "releasing twice should be idempotent"

    assert (
        tick(batches=batches, spec=spec, repo_specs=repo_specs, client=client).outcomes[0].status
        == "merged"
    )


def test_holds_survive_across_ticks(state_dir: Path, record: Path) -> None:
    """A hold with no release stays held; it is not forgotten next tick."""
    repo = "lake-of-rage"
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        holds=(ClusterHold(name="gate", lanes=("sales-*",)),),
    )
    repo_specs = {repo: spec_for(repo, merge_template=recorder(record))}
    client = StubClient({1: open_pr("aaaa111")})
    batches = [one_batch([pr(1, "aaaa111", lane="sales-1")])]
    for _ in range(3):
        assert (
            tick(batches=batches, spec=spec, repo_specs=repo_specs, client=client)
            .outcomes[0]
            .status
            == "held"
        )
    assert ran(record) == []


# ---------------------------------------------------------------------------
# Tick behaviour
# ---------------------------------------------------------------------------


def test_repo_without_a_merge_template_is_skipped_not_guessed(
    state_dir: Path, record: Path
) -> None:
    """No template means nothing runs; the executor never invents a command."""
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo)},  # no templates at all
        client=StubClient({1: open_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "skipped"
    assert "no merge template" in result.outcomes[0].detail
    assert ran(record) == []


def test_second_tick_with_nothing_new_does_nothing(state_dir: Path, record: Path) -> None:
    """A tick is idempotent: re-running it does not re-merge a merged PR."""
    repo = "lake-of-rage"
    spec = ExecutorSpec(state_dir=str(state_dir))
    repo_specs = {repo: spec_for(repo, merge_template=recorder(record))}
    batches = [one_batch([pr(1, "aaaa111")])]

    first = tick(
        batches=batches,
        spec=spec,
        repo_specs=repo_specs,
        client=StubClient({1: open_pr("aaaa111")}),
    )
    assert first.by_status("merged")
    assert len(ran(record)) == 1

    # GitHub now reports it merged; the next tick must not re-run the command.
    second = tick(
        batches=batches,
        spec=spec,
        repo_specs=repo_specs,
        client=StubClient({1: merged_pr("aaaa111")}),
    )
    assert second.by_status("skipped")
    assert len(ran(record)) == 1, "the merge command ran twice for one PR"


def test_dry_run_runs_no_commands_and_takes_no_lock(state_dir: Path, record: Path) -> None:
    """--dry-run reports the decision without acting on it."""
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111")}),
        dry_run=True,
    )
    assert result.by_status("merged")
    assert ran(record) == [], "dry run executed a command"
    assert not (state_dir / "deploy-lake-of-rage.lock").exists(), "dry run took a lock"


def test_deploy_then_verify_run_in_order(state_dir: Path, record: Path) -> None:
    """After a successful merge the deploy and verify commands both run."""
    repo = "lake-of-rage"
    repo_specs = {
        repo: spec_for(
            repo,
            merge_template=recorder(record, tag="merge"),
            deploy_template=recorder(record, tag="deploy"),
            verify_template=recorder(record, tag="verify"),
        )
    }
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=StubClient({1: open_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "merged"
    assert [c["tag"] for c in ran(record)] == ["merge", "deploy", "verify"]


def test_failed_deploy_stops_before_verify(state_dir: Path, record: Path) -> None:
    """A failed deploy must not be followed by a verify that assumes success."""
    repo = "lake-of-rage"
    repo_specs = {
        repo: spec_for(
            repo,
            merge_template=recorder(record, tag="merge"),
            deploy_template=recorder(record, exit_code=1, tag="deploy"),
            verify_template=recorder(record, tag="verify"),
        )
    }
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=StubClient({1: open_pr("aaaa111")}),
    )
    assert result.outcomes[0].status == "failed"
    assert [c["tag"] for c in ran(record)] == ["merge", "deploy"]
    assert "merge.failed" in result.events


def test_events_cover_the_happy_path(state_dir: Path, record: Path) -> None:
    """A successful tick emits the documented event sequence."""
    repo = "lake-of-rage"
    repo_specs = {
        repo: spec_for(
            repo,
            merge_template=recorder(record, tag="merge"),
            deploy_template=recorder(record, tag="deploy"),
            verify_template=recorder(record, tag="verify"),
        )
    }
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=StubClient({1: open_pr("aaaa111")}),
        run_id="test-run",
    )
    for event in (
        "merge.start",
        "merge.start_batch",
        "merge.merged",
        "merge.deployed",
        "merge.verified",
        "merge.end",
    ):
        assert event in result.events, f"{event} was never emitted"


def test_events_reach_the_run_log(state_dir: Path, record: Path) -> None:
    """Events go through the RunLog, not just onto the result object."""
    from agent_fleet.observability.log import RunLog

    runs = state_dir / "runs"
    run_log = RunLog.create(run_id="executor-test", runs_dir=runs, include_memory_ring=False)
    repo = "lake-of-rage"
    run_tick(
        plan=MergePlan(batches=(one_batch([pr(1, "aaaa111")]),)),
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111")}),
        run_log=run_log,
        run_id="executor-test",
    )
    events = [
        json.loads(line)["event"]
        for line in (runs / "executor-test.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "merge.merged" in events
    assert all("." in e for e in events), "event names must be dotted-namespace"


def test_render_text_summarises_the_tick(state_dir: Path, record: Path) -> None:
    """The operator-facing summary names each batch and its outcome."""
    repo = "lake-of-rage"
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: spec_for(repo, merge_template=recorder(record))},
        client=StubClient({1: open_pr("aaaa111")}),
    )
    text = result.render_text()
    assert "lake-of-rage" in text
    assert "1 merged" in text


# ---------------------------------------------------------------------------
# Config and command rendering
# ---------------------------------------------------------------------------


def test_executor_spec_round_trips() -> None:
    spec = parse_executor_spec(
        {
            "holds": [{"name": "g", "match": {"lanes": ["a-*"], "deploy_units": ["dbt"]}}],
            "exclusive_groups": [["x", "y"]],
            "post_merge_hold_seconds": 300,
            "rebase_command": "scripts/rebase.sh {pr}",
            "state_dir": "/tmp/state",
        }
    )
    assert spec.holds[0] == ClusterHold(name="g", lanes=("a-*",), deploy_units=("dbt",))
    assert spec.exclusive_groups == (("x", "y"),)
    assert spec.post_merge_hold_seconds == 300
    assert spec.state_dir == "/tmp/state"


def test_unknown_executor_key_is_rejected() -> None:
    """A typo must fail loudly, not silently disable a safety setting."""
    with pytest.raises(ValueError, match="unknown key"):
        parse_executor_spec({"post_merge_hold_second": 300})


def test_unknown_hold_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown key"):
        parse_executor_spec({"holds": [{"name": "g", "match": {"lane": ["a"]}}]})


def test_non_integer_seconds_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        parse_executor_spec({"post_merge_hold_seconds": "soon"})


def test_missing_executor_block_yields_inert_defaults() -> None:
    spec = parse_executor_spec(None)
    assert spec.holds == ()
    assert spec.exclusive_groups == ()
    assert spec.command_timeout_seconds == 1800


def test_command_argv_substitutes_and_splits() -> None:
    argv = command_argv("scripts/merge.sh {pr} {sha9}", pr="12", sha9="abc1234")
    assert argv == ["scripts/merge.sh", "12", "abc1234"]


def test_command_argv_keeps_quoted_arguments_together() -> None:
    """A quoted template argument stays one argv entry and is never re-parsed."""
    argv = command_argv('echo "two words" {pr}', pr="7")
    assert argv == ["echo", "two words", "7"]


def test_command_argv_does_not_use_a_shell() -> None:
    """Shell metacharacters are inert data, not syntax.

    Templates come from an operator's own config, but a command must never be
    able to smuggle a second command past the executor.  shlex only splits on
    whitespace, so ``;`` is an ordinary character and no shell ever sees it.
    """
    # Unquoted, the text is split, but never interpreted: `;` is just a byte.
    assert command_argv("echo {pr}", pr="1; rm -rf /") == ["echo", "1;", "rm", "-rf", "/"]
    # Quoting in the template keeps one argument whole, metacharacters and all.
    assert command_argv("echo '{pr}'", pr="a; b") == ["echo", "a; b"]


def test_merge_per_pr_template_runs_once_per_pr(state_dir: Path, record: Path) -> None:
    """A per-PR repo template chains one command per PR in batch order."""
    repo = "lake-of-rage"
    repo_specs = {repo: spec_for(repo, merge_per_pr_template=recorder(record) + " {pr} {sha9}")}
    result = tick(
        batches=[one_batch([pr(1, "aaaa111"), pr(2, "bbbb222")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=StubClient({1: open_pr("aaaa111"), 2: open_pr("bbbb222")}),
    )
    assert result.outcomes[0].status == "merged"
    assert [c["argv"] for c in ran(record)] == [["1", "aaaa111"], ["2", "bbbb222"]]


def test_group_for_returns_empty_for_an_ungrouped_repo() -> None:
    spec = parse_executor_spec({"exclusive_groups": [["a", "b"]]})
    assert spec.group_for("a") == ("a", "b")
    assert spec.group_for("zzz") == ()


def test_hold_matching_is_scoped_to_lane_and_unit() -> None:
    hold = ClusterHold(name="g", lanes=("sales-*",), deploy_units=("dbt",))
    assert hold.matches(lane="sales-1", deploy_unit="")
    assert hold.matches(lane="x", deploy_unit="dbt")
    assert not hold.matches(lane="x", deploy_unit="lor-api")


def test_tick_with_no_batches_is_a_no_op(state_dir: Path) -> None:
    result = tick(
        batches=[],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={},
        client=StubClient({}),
    )
    assert result.outcomes == ()
    assert "merge.start" in result.events and "merge.end" in result.events


def test_executor_reports_the_merge_commit_to_deploy(state_dir: Path, record: Path) -> None:
    """Deploy and verify address the merge commit, not the reviewed PR head."""
    repo = "lake-of-rage"
    repo_specs = {
        repo: spec_for(
            repo,
            merge_template=recorder(record, tag="merge"),
            deploy_template=recorder(record, tag="deploy") + " {merge_sha}",
        )
    }
    client = StubClient({1: {**open_pr("aaaa111"), "mergeCommit": {"oid": "feedc0ffee"}}})
    result = tick(
        batches=[one_batch([pr(1, "aaaa111")])],
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=client,
    )
    assert result.outcomes[0].merged_sha == "feedc0ffee"
    deploy = [c for c in ran(record) if c["tag"] == "deploy"]
    assert deploy and deploy[0]["argv"] == ["feedc0ffee"]


def test_executor_uses_a_real_github_client_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no injected client the executor builds a real GitHubClient.

    Production never injects one, so this is the path that actually runs; the
    test pins that the client is constructed rather than ``None`` being passed
    through and blowing up later on the first ``pr_detail`` call.
    """
    import agent_fleet.merge_plan.execute as execute_mod

    created: list[object] = []

    class Recorder:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            created.append(self)

        def pr_detail(self, _pr_number: int) -> dict[str, object]:
            return {}

        def for_repo(self, _repo_path: Path | None) -> Recorder:
            return self

    monkeypatch.setattr(execute_mod, "GitHubClient", Recorder)
    run_tick(
        plan=MergePlan(batches=()),
        spec=ExecutorSpec(state_dir=str(tmp_path / "state")),
        repo_specs={},
        client=None,
    )
    assert created, "run_tick did not construct a GitHub client"
