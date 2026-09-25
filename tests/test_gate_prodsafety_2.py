"""A PR approved under two repo spellings must be planned once, not twice.

`build_plan` de-duplicates approvals *before* `_normalize_repo` rewrites a
repo name onto the key used in `repo_specs`.  So the same PR recorded as
`owner/name#42` in a status file and as `name` in the lane registry has two
different raw repo strings, survives `dedupe_approvals` as two entries, and is
only collapsed afterwards — into a name, not into one approval.  Both copies
then reach the planner and the operator's merge script is handed the same PR
twice in one batch.

`collect_from_status_dir` only recognises `owner/name#42` references, while a
lane record may carry the bare `name`, so this pairing is what the two
collectors actually produce for one approved PR.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.merge_plan.collect import GitHubClient
from agent_fleet.merge_plan.config import builtin_spec
from agent_fleet.merge_plan.plan import build_plan

HEAD_SHA = "abc1234def012"


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake `gh` on PATH reporting PR 42 as approved and still current."""
    data_file = tmp_path / "gh_data.json"
    data_file.write_text(
        json.dumps(
            {
                "42": {
                    "headRefOid": HEAD_SHA,
                    "baseRefName": "main",
                    "additions": 5,
                    "deletions": 1,
                    "files": [{"path": "api/src/x.py"}],
                }
            }
        ),
        encoding="utf-8",
    )
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"data = json.load(open({str(data_file)!r}))\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['pr', 'view'] and args[2] in data:\n"
        "    print(json.dumps(data[args[2]]))\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('no such pr\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_same_pr_under_two_repo_spellings_is_planned_once(
    tmp_path: Path, fake_gh: None
) -> None:
    lanes = tmp_path / "lanes"
    (lanes / "op").mkdir(parents=True)
    # The lane registry recorded the repo bare...
    (lanes / "op" / "lane1.json").write_text(
        json.dumps(
            {
                "lane": "lane1",
                "operator": "op",
                "repo": "lake-of-rage",
                "pr": 42,
                "status_line": f"PREMERGE-APPROVED {HEAD_SHA}",
            }
        ),
        encoding="utf-8",
    )
    # ...and the status file recorded it owner-qualified, for the same PR.
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "gate.txt").write_text(
        f"Evan-Kim2028/lake-of-rage#42\nPREMERGE-APPROVED {HEAD_SHA}\n", encoding="utf-8"
    )

    spec = builtin_spec("lake-of-rage")
    spec.merge_template = "lake_batch_merge.sh {pr_args}"
    plan = build_plan(
        repo_specs={"lake-of-rage": spec},
        operator="op",
        status_dir=status_dir,
        lanes_root=lanes,
        client=GitHubClient(),
        check_merges=False,
    )

    planned = [(b.repo, p.pr_number) for b in plan.batches for p in b.prs]
    # One approval, one planned merge — the repo name is reconciled by
    # _normalize_repo, so both records name the same PR.
    assert planned == [("lake-of-rage", 42)], f"PR 42 planned more than once: {planned}"

    commands = [c for b in plan.batches for c in b.executor_commands]
    assert commands == ["lake_batch_merge.sh 42:abc1234de"], (
        f"merge script handed a duplicate PR: {commands}"
    )


def test_build_plan_does_not_double_count_size(tmp_path: Path, fake_gh: None) -> None:
    """The batch size / cap accounting must see the PR once too."""
    lanes = tmp_path / "lanes"
    (lanes / "op").mkdir(parents=True)
    (lanes / "op" / "lane1.json").write_text(
        json.dumps(
            {
                "lane": "lane1",
                "operator": "op",
                "repo": "lake-of-rage",
                "pr": 42,
                "status_line": f"PREMERGE-APPROVED {HEAD_SHA}",
            }
        ),
        encoding="utf-8",
    )
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "gate.txt").write_text(
        f"Evan-Kim2028/lake-of-rage#42\nPREMERGE-APPROVED {HEAD_SHA}\n", encoding="utf-8"
    )

    spec = builtin_spec("lake-of-rage")
    spec.merge_template = "lake_batch_merge.sh {pr_args}"
    plan = build_plan(
        repo_specs={"lake-of-rage": spec},
        operator="op",
        status_dir=status_dir,
        lanes_root=lanes,
        client=GitHubClient(),
        check_merges=False,
    )

    assert plan.to_dict()["merged_pr_count"] == 1
    for batch in plan.batches:
        assert batch.size == 1, f"batch {batch.index} size counted PR 42 twice"
