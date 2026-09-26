"""Regression: the executor's PR-state fields are never actually requested.

The existing executor tests all inject a ``StubClient`` whose ``pr_detail``
returns hand-written dicts already containing ``state`` and ``mergeable``.  That
hides the one thing the real client cannot do: ``GitHubClient.pr_detail`` asks
``gh`` for a fixed field set, and ``gh`` returns *only* the fields you ask for.

``gh pr view <n> --json a,b,c`` emits exactly the keys ``a``, ``b``, ``c`` -- it
never adds fields you did not request.  So when the production field set omits
``state``/``mergeable``/``mergeCommit``, the executor reads ``""`` for all of
them, ``_partition_prs`` classifies every PR as "state unknown", and the whole
merge queue is skipped -- silently, with a green exit code, every tick, forever.

This test uses the *real* ``GitHubClient`` against a fake ``gh`` that models
GitHub's actual contract (requested-keys-only) for a genuinely open, mergeable,
approved PR.  The merge command is a real subprocess that writes a real file, so
"it merged" is an observation rather than a mock assertion.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.merge_plan.collect import GitHubClient
from agent_fleet.merge_plan.execute import run_tick
from agent_fleet.merge_plan.types import (
    ApprovedPR,
    Batch,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)

HEAD_SHA = "aaaa111aaaa111aaaa111aaaa111aaaa111aaaa11"
MERGE_SHA = "bbbb222bbbb222bbbb222bbbb222bbbb222bbbb22"

#: Every field GitHub knows about this PR.  ``gh --json`` returns the subset the
#: caller asked for; this map is the full truth the fake binary filters down to.
PR_TRUTH: dict[str, Any] = {
    "state": "OPEN",
    "mergeable": "MERGEABLE",
    "headRefOid": HEAD_SHA,
    "baseRefName": "main",
    "additions": 10,
    "deletions": 2,
    "files": [{"path": "api/thing.py"}],
    "mergeCommit": {"oid": MERGE_SHA},
}

#: The fields the executor's own code reads back out of the payload.
REQUIRED_FIELDS = ("state", "mergeable", "mergeCommit")


def _gh_stub(bin_dir: Path) -> Path:
    """A ``gh`` stand-in that reproduces ``gh pr view --json`` exactly.

    ``gh`` validates the requested field names and, on success, emits a JSON
    object holding *only* those keys -- so a field the caller never asked for is
    simply absent from the payload, however true it is on GitHub.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    truth = bin_dir / "truth.json"
    truth.write_text(json.dumps(PR_TRUTH), encoding="utf-8")

    stub = bin_dir / "gh"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"truth = json.load(open({str(truth)!r}, encoding='utf-8'))\n"
        "args = sys.argv[1:]\n"
        "if args[:2] != ['pr', 'view']:\n"
        "    sys.exit(0 if args[:2] == ['pr', 'list'] else 1)\n"
        "wanted = args[args.index('--json') + 1].split(',')\n"
        "unknown = [f for f in wanted if f not in truth]\n"
        "if unknown:\n"
        "    sys.stderr.write('unknown field: ' + ', '.join(unknown) + '\\n')\n"
        "    sys.exit(1)\n"
        "json.dump({f: truth[f] for f in wanted}, sys.stdout)\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


@pytest.fixture
def gh_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fake ``gh`` first on PATH so the real client shells out to it."""
    bin_dir = tmp_path / "bin"
    _gh_stub(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


@pytest.fixture
def merge_recorder(tmp_path: Path) -> Path:
    """File the real merge command appends to, so 'it ran' is observable."""
    return tmp_path / "merged.jsonl"


def _merge_command(record: Path) -> str:
    """A merge command that records its invocation, then exits 0."""
    runner = record.parent / "merge.sh"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(
        "import json, sys\n"
        f"with open({str(record)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:]}) + '\\n')\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {runner}"


def _repo_spec(repo: str, record: Path) -> RepoSpec:
    return RepoSpec(
        name=repo,
        merge_template=_merge_command(record),
        deploy_template="",
        verify_template="",
    )


def _ran(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_pr_detail_requests_the_fields_the_executor_reads(gh_on_path: Path) -> None:
    """``pr_detail`` must fetch the fields the executor decides on.

    This is the root cause in isolation: the executor branches on ``state`` and
    ``mergeable`` and records ``mergeCommit``, so the client that feeds it has to
    ask ``gh`` for exactly those keys.
    """
    client = GitHubClient()
    detail = client.pr_detail(1)

    missing = [f for f in REQUIRED_FIELDS if f not in detail]
    assert not missing, (
        "GitHubClient.pr_detail does not request "
        f"{missing}, so the executor's _pr_state/_partition_prs/_merged_sha read "
        "nothing for them and no PR is ever classified mergeable"
    )


def test_approved_open_mergeable_pr_actually_merges(
    gh_on_path: Path, tmp_path: Path, merge_recorder: Path
) -> None:
    """An approved, open, mergeable PR must reach the merge command.

    Driven through the real ``GitHubClient`` and the real ``subprocess`` call,
    with a fake ``gh`` that behaves like GitHub: the PR *is* open and mergeable,
    and the payload contains only the keys the client requested.
    """
    repo = "lake-of-rage"
    state_dir = tmp_path / "merge-state"
    state_dir.mkdir()
    pr = ApprovedPR(
        repo=repo,
        pr_number=7,
        approved_sha=HEAD_SHA,
        head_sha=HEAD_SHA,
        lane="pass2-api",
    )
    plan = MergePlan(
        batches=(Batch(index=0, repo=repo, prs=(pr,), deploy_unit="lor-api", executor_commands=()),)
    )

    result = run_tick(
        plan=plan,
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs={repo: _repo_spec(repo, merge_recorder)},
        client=GitHubClient(),
        run_id="correctness-1",
    )

    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status == "merged", (
        f"approved open mergeable PR was not merged (status={outcome.status!r}, "
        f"detail={outcome.detail!r}); the client never asked gh for 'state'/'mergeable'"
    )
    assert _ran(merge_recorder), "the merge command never ran"
