"""Regression test for spec-3: one GitHubClient is built with a single cwd.

``build_plan`` constructs a single ``GitHubClient`` with ``cwd`` set to the
first repo's path and hands that one client to ``profile_approvals`` for every
approval.  ``gh pr view <n>`` resolves the PR number against the repository
containing the cwd, so a PR belonging to any *other* selected repo is looked
up in the wrong repository.

The fake ``gh`` below answers per-repository, keyed on the basename of its
cwd, exactly as the real binary does.  Both repos hold a genuinely approved
(and non-stale) PR numbered 101, with different head SHAs, so a correct
implementation plans both and only a cwd-blind client can collapse them.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_fleet.merge_plan import build_plan, resolve_repo_specs

#: Distinct real-looking SHAs so the two repositories' PR #101 differ only by
#: which repository it is looked up in.
LOR_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
SILPH_SHA = "0f9e8d7c6b5a4938271605f4e3d2c1b0a9f8e7d6c"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )


@pytest.fixture
def two_repos(tmp_path: Path) -> dict[str, Path]:
    """Two real checkouts, lake-of-rage and silphcoanalytics, no `origin`."""
    repos = {}
    for name in ("lake-of-rage", "silphcoanalytics"):
        path = tmp_path / name
        path.mkdir()
        _git("init", "-q", "-b", "main", cwd=path)
        _git("config", "user.email", "t@example.com", cwd=path)
        _git("config", "user.name", "T", cwd=path)
        (path / "README.md").write_text("base\n", encoding="utf-8")
        _git("add", ".", cwd=path)
        _git("commit", "-q", "-m", "base", cwd=path)
        repos[name] = path
    return repos


@pytest.fixture
def per_repo_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake ``gh pr view`` that answers per repository, keyed on its cwd.

    Mirrors the real tool: the PR number is looked up in whichever repository
    the process is running inside, so PR 101 of one repo is a *different* PR
    from PR 101 of another.
    """
    data_file = tmp_path / "gh_repos.json"
    data_file.write_text(
        json.dumps(
            {
                "lake-of-rage": {
                    "101": {
                        "headRefOid": LOR_SHA,
                        "baseRefName": "main",
                        "additions": 3,
                        "deletions": 1,
                        "files": [{"path": "api/routes/orders.py"}],
                    }
                },
                "silphcoanalytics": {
                    "101": {
                        "headRefOid": SILPH_SHA,
                        "baseRefName": "main",
                        "additions": 5,
                        "deletions": 1,
                        "files": [{"path": "api/routes/ingest.py"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"data = json.load(open({str(data_file)!r}))\n"
        "args = sys.argv[1:]\n"
        "if args[:2] != ['pr', 'view']:\n"
        "    sys.exit(1)\n"
        "repo = os.path.basename(os.getcwd())\n"
        "entry = data.get(repo, {}).get(args[2])\n"
        "if entry is None:\n"
        "    sys.stderr.write('no such pr in ' + repo + '\\n')\n"
        "    sys.exit(1)\n"
        "print(json.dumps(entry))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_each_repo_is_profiled_in_its_own_checkout(
    two_repos: dict[str, Path],
    per_repo_gh: None,
    tmp_path: Path,
) -> None:
    """Two selected repos, one approved PR each: both must reach the plan.

    The approval for silphcoanalytics matches its own PR head, so it is not
    stale and its head is readable.  Resolving PR 101 against lake-of-rage's
    checkout instead yields a head that does not match, which is a false
    "stale approval" and drops the repo from the plan entirely.
    """
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "lor.txt").write_text(
        f"PREMERGE-APPROVED {LOR_SHA} org/lake-of-rage#101\n", encoding="utf-8"
    )
    (status_dir / "silph.txt").write_text(
        f"PREMERGE-APPROVED {SILPH_SHA} org/silphcoanalytics#101\n", encoding="utf-8"
    )

    repo_specs = resolve_repo_specs(
        [str(two_repos["lake-of-rage"]), str(two_repos["silphcoanalytics"])],
        fleet_config_path=tmp_path / "nonexistent-fleet.yaml",
    )
    assert set(repo_specs) == {"lake-of-rage", "silphcoanalytics"}

    plan = build_plan(
        repo_specs=repo_specs,
        status_dir=status_dir,
        lanes_root=tmp_path / "no-lanes",
        max_batch_size=5,
        check_merges=False,
    )

    planned = {(b.repo, p.pr_number) for b in plan.batches for p in b.prs}
    assert planned == {
        ("lake-of-rage", 101),
        ("silphcoanalytics", 101),
    }, (
        "the plan dropped an approved, non-stale PR: "
        f"batches={[(b.repo, [p.pr_number for p in b.prs]) for b in plan.batches]} "
        f"excluded={[(e.repo, e.pr_number, e.reason) for e in plan.excluded]}"
    )

    assert not [
        e
        for e in plan.excluded
        if e.repo == "silphcoanalytics" and e.pr_number == 101
    ], "silphcoanalytics#101 was excluded despite its approval matching its own head"
