"""A deploy that fails after the merge landed must not be forgotten.

The executor's contract (docs/MERGE-PLAN.md) is that a queue which stops
moving says why.  This pins the hard case: the merge command *succeeded* --
GitHub reports the PR MERGED from then on -- but the deploy command that
follows it failed.  The work is on the main branch and nothing is shipping it.

From that point on the PR can never become eligible again, so every later tick
sees "already merged" and reports ``skipped`` with exit code 0.  A production
deploy that died is therefore never retried, never re-alerted, and leaves no
record anywhere an operator can see it -- the queue looks perfectly healthy
forever.

The test runs three consecutive ticks against a real subprocess deploy command
that exits 3, flipping the stub client to MERGED after the first tick exactly
as GitHub would, and requires the failure to stay visible.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_fleet.merge_plan.execute import TickResult, run_tick
from agent_fleet.merge_plan.types import (
    ApprovedPR,
    Batch,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)

REPO = "alpha"


class StubClient:
    """GitHubClient stand-in whose PR state the test controls directly."""

    def __init__(self, prs: dict[int, dict[str, object]]) -> None:
        self._prs = prs

    def pr_detail(self, pr_number: int) -> dict[str, object]:
        return self._prs.get(pr_number, {})

    def for_repo(self, repo_path: Path | None) -> StubClient:
        del repo_path
        return self


def _open_pr(sha: str) -> dict[str, object]:
    return {"state": "OPEN", "headRefOid": sha, "mergeable": "MERGEABLE", "mergeCommit": None}


def _merged_pr(sha: str) -> dict[str, object]:
    # What `gh pr view` returns once the merge command actually landed: the
    # branch is on main, but the deploy it triggered never happened.
    return {
        "state": "MERGED",
        "headRefOid": sha,
        "mergeable": "UNKNOWN",
        "mergeCommit": {"oid": "feedc0ffee"},
    }


def _recorder(record: Path, *, tag: str, exit_code: int = 0) -> str:
    """A real subprocess command that logs its invocation, then exits."""
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


def _ran(record: Path) -> list[dict[str, object]]:
    if not record.exists():
        return []
    return [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _exit_code(result: TickResult) -> int:
    """The exit code `fleet merge run` derives from a tick (cli._report)."""
    return 1 if result.by_status("failed") else 0


def test_failed_deploy_after_a_landed_merge_keeps_being_reported(tmp_path: Path) -> None:
    """Tick 0 fails the deploy; the merge already landed, so ticks 1+ must not go quiet.

    Before the fix every later tick reports ``skipped (#1: already merged)``
    and exits 0, so a broken production deploy disappears from view the moment
    the merge succeeds -- the opposite of "a queue that is not moving says why".
    """
    state_dir = tmp_path / "merge-state"
    state_dir.mkdir()
    record = tmp_path / "ran.jsonl"

    repo_spec = RepoSpec(
        name=REPO,
        path="",
        merge_template=_recorder(record, tag="merge"),
        deploy_template=_recorder(record, tag="deploy", exit_code=3),
        verify_template=_recorder(record, tag="verify"),
    )
    spec = ExecutorSpec(state_dir=str(state_dir))
    batch = Batch(
        index=0,
        repo=REPO,
        prs=(ApprovedPR(repo=REPO, pr_number=1, approved_sha="aaaa111", head_sha="aaaa111"),),
        deploy_unit="alpha-api",
        executor_commands=(),
    )

    def tick(client: StubClient) -> TickResult:
        return run_tick(
            plan=MergePlan(batches=(batch,)),
            spec=spec,
            repo_specs={REPO: repo_spec},
            client=client,
            run_id="gate-j-1",
        )

    first = tick(StubClient({1: _open_pr("aaaa111")}))
    assert first.outcomes[0].status == "failed", first.outcomes[0].to_dict()
    assert "deploy" in first.outcomes[0].detail
    assert [c["tag"] for c in _ran(record)] == ["merge", "deploy"]
    assert "verify" not in [c["tag"] for c in _ran(record)], (
        "verify must not run after a failed deploy"
    )

    # GitHub now says the PR is merged -- the merge command really did land it.
    # The deploy is still broken, and production has still never seen this work.
    merged_client = StubClient({1: _merged_pr("aaaa111")})

    later = [tick(merged_client) for _ in range(2)]
    for tick_no, result in enumerate(later, start=1):
        outcome = result.outcomes[0]
        assert outcome.status != "skipped", (
            f"tick {tick_no} reported {outcome.status!r} ({outcome.detail!r}): the failed "
            f"deploy is forgotten, because a merged PR is never eligible again"
        )
        assert outcome.status == "failed", (
            f"tick {tick_no} must keep reporting the broken deploy, got {outcome.to_dict()!r}"
        )
        assert _exit_code(result) == 1, f"tick {tick_no} exits 0, so cron never alerts again"

    # Either the deploy is retried, or at minimum the failure is still reported;
    # what must not happen is the operator being told everything is fine.
    assert _exit_code(later[-1]) == 1
