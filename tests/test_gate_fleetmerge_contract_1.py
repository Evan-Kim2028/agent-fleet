"""A configured ``path: ~/...`` must actually work for the merge executor.

docs/MERGE-PLAN.md:251 documents exactly this form::

    merge_plan:
      repos:
        - name: lake-of-rage
          path: ~/Documents/lake-of-rage
          merge_template: "scripts/lake_batch_merge.sh {pr_args}"

The plan, the profiles and the mergeability check all read that path through
``Path(...).expanduser()``, but the executor hands the *raw* string to
``subprocess.Popen(cwd=...)``.  ``Popen`` does no tilde expansion, so the spawn
dies with ``FileNotFoundError``, which ``_run_command`` maps to rc=127, and the
batch is reported ``failed`` -- forever, for every repo configured the way the
docs say to configure it.

The test drives the real code path: a real fleet.yaml on disk, the real
``resolve_repo_specs``, the real ``run_tick``, and real subprocesses that record
the cwd they ran in.  No stub of the executor, no mock of ``Popen``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.merge_plan.config import resolve_repo_specs
from agent_fleet.merge_plan.execute import run_tick
from agent_fleet.merge_plan.types import (
    ApprovedPR,
    Batch,
    ExecutorSpec,
    MergePlan,
)

REPO = "lake-of-rage"


class StubClient:
    """PR state as a plain dict: the executor only needs ``pr_detail``."""

    def __init__(self, prs: dict[int, dict[str, Any]]) -> None:
        self._prs = prs

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        return self._prs.get(pr_number, {})

    def for_repo(self, repo_path: Path | None) -> StubClient:
        del repo_path
        return self


def _recorder(record: Path, tag: str = "merge") -> str:
    """A command that appends one JSON line -- cwd included -- then exits 0."""
    runner = record.parent / f"runner-{tag}.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(
        "import json, os, sys\n"
        f"record, tag = {str(record)!r}, {tag!r}\n"
        "with open(record, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'tag': tag, 'cwd': os.getcwd(), "
        "'argv': sys.argv[1:]}) + '\\n')\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {runner}"


def _ran(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]


def test_tilde_repo_path_from_config_lets_the_merge_command_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented ``path: ~/Documents/<repo>`` form must run the commands.

    HOME is redirected so ``~`` is unambiguous, and the checkout really exists
    under it -- the only thing standing between the documented config and a
    merged PR is the executor's own path handling.
    """
    home = tmp_path / "home"
    checkout = home / "Documents" / REPO
    checkout.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # posix uses HOME; be explicit

    record = tmp_path / "ran.jsonl"
    state_dir = tmp_path / "merge-state"
    state_dir.mkdir()

    config = tmp_path / "fleet.yaml"
    config.write_text(
        "merge_plan:\n"
        "  repos:\n"
        f"    - name: {REPO}\n"
        f"      path: ~/Documents/{REPO}\n"
        f'      merge_template: "{_recorder(record)}"\n'
        '      deploy_template: "'
        f'{_recorder(record, tag="deploy")}"\n'
        '      verify_template: "'
        f'{_recorder(record, tag="verify")}"\n',
        encoding="utf-8",
    )

    repo_specs = resolve_repo_specs([], fleet_config_path=config)
    assert REPO in repo_specs, "the documented config form did not parse into a repo spec"

    sha = "aaaa111"
    batch = Batch(
        index=0,
        repo=REPO,
        prs=(ApprovedPR(repo=REPO, pr_number=1, approved_sha=sha, head_sha=sha, lane=""),),
        deploy_unit="lor-api",
        executor_commands=(),
    )
    result = run_tick(
        plan=MergePlan(batches=(batch,)),
        spec=ExecutorSpec(state_dir=str(state_dir)),
        repo_specs=repo_specs,
        client=StubClient({1: {"state": "OPEN", "headRefOid": sha, "mergeable": "MERGEABLE"}}),
        run_id="contract-1",
        now=time.time,
    )

    outcome = result.outcomes[0]
    assert outcome.status == "merged", (
        f"a repo configured as `path: ~/{REPO}` failed: {outcome.status}: {outcome.detail}"
    )

    # The commands must run inside the expanded checkout, not merely succeed.
    tags = [c["tag"] for c in _ran(record)]
    assert tags == ["merge", "deploy", "verify"], tags
    for entry in _ran(record):
        assert os.path.realpath(entry["cwd"]) == os.path.realpath(checkout), (
            f"{entry['tag']} ran in {entry['cwd']}, not the expanded repo path"
        )
