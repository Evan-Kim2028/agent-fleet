"""Regression test: ``--dry-run`` must not mutate durable executor state.

Claim under test (correctness-2, ``agent_fleet/merge_plan/execute.py:1069``):

    ``_run_batch`` records post-merge hold state into the durable ledger with
    no ``dry_run`` guard, so ``--dry-run`` mutates executor state and sets a
    hold that blocks the real merge for ``post_merge_hold_seconds``.

``run_tick(dry_run=True)`` is meant only to *report* what would happen.  A dry
run must not take a deploy lock (that is already covered elsewhere), and it
must not write ``record_served`` into ``<state_dir>/ledger.json`` either -- a
simulated merge never deployed, so nothing about the exclusive group's turn
taking or its post-merge quiet period should advance on disk.

This drives the real public entrypoint ``run_tick`` twice against the same
durable state: first a dry run, then the real run it is supposed to predict.
No network, no real repository, and the only subprocesses are inert recorder
commands, so the failure is purely an assertion about wrong behaviour.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

from agent_fleet.merge_plan.execute import HoldLedger, run_tick
from agent_fleet.merge_plan.types import (
    ApprovedPR,
    Batch,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)

# ---------------------------------------------------------------------------
# Helpers (self-contained; mirrors the style of the sibling executor tests)
# ---------------------------------------------------------------------------


class StubClient:
    """A GitHubClient stand-in whose PR state is a plain dict."""

    def __init__(self, prs: dict[int, dict[str, Any]]) -> None:
        self._prs = prs

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        return self._prs.get(pr_number, {})

    def for_repo(self, repo_path: Path | None) -> StubClient:
        del repo_path
        return self


def open_pr(sha: str, *, mergeable: str = "MERGEABLE") -> dict[str, Any]:
    return {"state": "OPEN", "headRefOid": sha, "mergeable": mergeable}


def recorder(record: Path, *, exit_code: int = 0, tag: str = "merge") -> str:
    """A command that logs that it ran, then exits *exit_code*."""
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


@pytest.fixture
def record(tmp_path: Path) -> Path:
    """Where recorder commands append one JSON line per invocation."""
    return tmp_path / "ran.jsonl"


def spec_for(repo: str, **fields: str) -> RepoSpec:
    spec = RepoSpec(name=repo)
    for key, value in fields.items():
        setattr(spec, key, value)
    return spec


def pr(number: int, sha: str, *, repo: str = "lake-of-rage", lane: str = "") -> ApprovedPR:
    return ApprovedPR(repo=repo, pr_number=number, approved_sha=sha, head_sha=sha, lane=lane)


def one_batch(
    prs: Sequence[ApprovedPR], *, index: int = 0, repo: str = "lake-of-rage", unit: str = "lor-api"
) -> Batch:
    return Batch(index=index, repo=repo, prs=tuple(prs), deploy_unit=unit, executor_commands=())


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_post_merge_hold_into_durable_ledger(
    tmp_path: Path, record: Path
) -> None:
    """A dry run of an exclusive-group repo must leave the ledger untouched.

    Two repos share an exclusive group with a 300s post-merge hold.  A dry run
    predicts that repo ``a`` would merge and the group would then go quiet.
    That prediction is reported, never enacted: the ledger must still say the
    group has never served anyone, so the *real* run afterwards is free to
    merge and is not spuriously held by a hold that only ever existed in a
    simulation.
    """
    a, b = "lake-of-rage", "silphcoanalytics"
    state_dir = tmp_path / "merge-state"
    state_dir.mkdir()

    clock = {"t": 1000.0}
    spec = ExecutorSpec(
        state_dir=str(state_dir),
        exclusive_groups=((a, b),),
        post_merge_hold_seconds=300,
    )
    repo_specs = {
        a: spec_for(a, merge_template=recorder(record, tag="merge")),
        b: spec_for(b, merge_template=recorder(record, tag="merge")),
    }
    client = StubClient({1: open_pr("aaaa111"), 2: open_pr("bbbb222")})
    batches = [
        one_batch([pr(1, "aaaa111")], index=0, repo=a),
        one_batch([pr(2, "bbbb222")], index=1, repo=b),
    ]
    ledger_path = state_dir / "ledger.json"

    def tick(*, dry_run: bool) -> Any:
        return run_tick(
            plan=MergePlan(batches=tuple(batches)),
            spec=spec,
            repo_specs=repo_specs,
            client=client,
            dry_run=dry_run,
            run_id="gate-test",
            now=lambda: clock["t"],
        )

    # 1. A dry run predicts the outcome without running any command...
    dry = tick(dry_run=True)
    assert [o.status for o in dry.outcomes] == ["merged", "held"]
    assert ran(record) == [], "dry run executed a real command"

    # 2. ...and, crucially, must not have enacted the post-merge hold.  A
    #    simulated merge never deployed, so the durable ledger must still show
    #    the group as never having served anyone.
    assert not ledger_path.exists(), (
        "dry run wrote the durable ledger; a simulated merge must not record a "
        "served turn or a post-merge hold"
    )
    ledger = HoldLedger(ledger_path)
    group = (a, b)
    assert ledger.hold_until(group) <= clock["t"], (
        "dry run persisted a post-merge hold into the durable ledger; the real "
        "merge that follows is now blocked for post_merge_hold_seconds"
    )
    assert ledger.last_served(group) == "", (
        "dry run recorded a served turn; nothing was deployed, so the group "
        "must not have given up its turn"
    )

    # 3. The real run is therefore not held by a phantom hold and actually
    #    merges (a is first, b yields by strict alternation).
    real = tick(dry_run=False)
    assert [o.status for o in real.outcomes] == ["merged", "held"]
    assert real.outcomes[1].status == "held" and "went last" in real.outcomes[1].detail, (
        "the real tick must be gated by genuine alternation, not by a hold that "
        "only a dry run ever created"
    )
    assert [c["tag"] for c in ran(record)] == ["merge"], "the real run did not merge"
