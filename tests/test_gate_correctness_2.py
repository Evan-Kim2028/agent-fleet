"""build_plan must resolve each PR number against *its own* repository.

`build_plan` builds a single `GitHubClient(cwd=_first_repo_path(repo_specs))`
and hands it to `profile_approvals`, which calls `client.pr_detail(pr_number)`
for every approval without telling the client which repo the PR belongs to.
`gh pr view <n>` resolves the number against the origin remote of the checkout
it is run in, so with more than one `--repo-path` every repo after the
alphabetically-first one is looked up in the wrong repository: its PRs are
either unreadable or, worse, silently profiled with another repo's files,
deploy unit and head SHA.

The fake `gh` below models that contract exactly: it reads `git remote get-url
origin` from its own cwd to decide which repository a PR number refers to.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.merge_plan.config import builtin_spec
from agent_fleet.merge_plan.plan import build_plan

# ---------------------------------------------------------------------------
# Fixtures: two real checkouts with distinct origin remotes, and a fake `gh`
# that resolves PR numbers against the origin of whichever repo it is run in.
# ---------------------------------------------------------------------------

_FAKE_GH = """#!/usr/bin/env python3
import json, os, subprocess, sys

data = json.load(open({data_file!r}))
args = sys.argv[1:]

if args[:2] != ['pr', 'view']:
    sys.exit(1)

# Real `gh` resolves a bare PR number against the origin remote of the
# checkout it runs in, so the repository identity comes from cwd.
result = subprocess.run(
    ['git', 'remote', 'get-url', 'origin'],
    capture_output=True, text=True, cwd=os.getcwd(),
)
if result.returncode != 0:
    sys.stderr.write('not a git repository\\n')
    sys.exit(1)
remote = result.stdout.strip()
repo = remote.rsplit('/', 1)[-1]
if repo.endswith('.git'):
    repo = repo[:-len('.git')]

entry = data.get(repo + '#' + args[2])
if entry is None:
    sys.stderr.write('no such pull request in ' + repo + '\\n')
    sys.exit(1)
print(json.dumps(entry))
sys.exit(0)
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout.strip()


def _make_checkout(root: Path, name: str) -> Path:
    """A repo directory whose ``origin`` remote identifies *name*."""
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "remote", "add", "origin", f"git@github.com:acme/{name}.git")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """A fake ``gh`` on PATH whose answers depend on the cwd it is run in."""
    data_file = tmp_path / "gh_data.json"
    data_file.write_text("{}", encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(_FAKE_GH.format(data_file=str(data_file)), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "fleet-home"))
    monkeypatch.setenv("AGENT_FLEET_CONFIG", str(tmp_path / "absent-fleet.yaml"))
    return data_file


def _write_status(status_dir: Path, repo: str, pr: int, approved_sha: str) -> None:
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / f"{repo}-{pr}.txt").write_text(
        f"acme/{repo}#{pr}\nPREMERGE-APPROVED {approved_sha}\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_pr_resolved_against_its_own_repo_not_the_first_one(tmp_path: Path, fake_gh: Path) -> None:
    """A PR number that only exists in the *second* repo must still batch.

    lake-of-rage sorts first, so it is what `_first_repo_path` returns and
    what the shared GitHubClient is bound to. lake-of-rage has no PR 7, so
    running `gh pr view 7` there fails; the correct answer is that
    silphcoanalytics#7 profiles from *its own* files and deploy unit.
    """
    lake = _make_checkout(tmp_path, "lake-of-rage")
    silph = _make_checkout(tmp_path, "silphcoanalytics")

    fake_gh.write_text(
        json.dumps(
            {
                "lake-of-rage#1": {
                    "headRefOid": "aaaa111aaaa",
                    "baseRefName": "main",
                    "additions": 5,
                    "deletions": 1,
                    "files": [{"path": "api/src/LOR-PR1.py"}],
                },
                "silphcoanalytics#7": {
                    "headRefOid": "bbbb222bbbb",
                    "baseRefName": "main",
                    "additions": 4,
                    "deletions": 2,
                    "files": [{"path": "frontend/src/x.tsx"}],
                },
            }
        ),
        encoding="utf-8",
    )

    status = tmp_path / "status"
    _write_status(status, "lake-of-rage", 1, "aaaa111")
    _write_status(status, "silphcoanalytics", 7, "bbbb222")

    repo_specs = {
        "lake-of-rage": builtin_spec("lake-of-rage", str(lake)),
        "silphcoanalytics": builtin_spec("silphcoanalytics", str(silph)),
    }

    plan = build_plan(
        repo_specs=repo_specs,
        status_dir=status,
        check_merges=False,
    )

    by_repo = {b.repo: b for b in plan.batches}
    assert "silphcoanalytics" in by_repo, (
        f"silphcoanalytics#7 was not batched; excluded="
        f"{[(p.repo, p.pr_number, p.reason) for p in plan.excluded]}"
    )
    batch = by_repo["silphcoanalytics"]
    assert [p.pr_number for p in batch.prs] == [7]
    # Profiled from silphcoanalytics' own files, not lake-of-rage's.
    assert batch.deploy_unit == "frontend"


def test_colliding_pr_number_profiles_own_repo_files(tmp_path: Path, fake_gh: Path) -> None:
    """When both repos have a #7, each must profile its *own* changed files.

    Both heads share the approved prefix so neither is stale; the only way
    the plans differ is which repository gh was asked. Sharing the prefix
    keeps the stale-approval verdict identical in both repos, isolating the
    file/deploy-unit resolution as the only observable difference.
    """
    lake = _make_checkout(tmp_path, "lake-of-rage")
    silph = _make_checkout(tmp_path, "silphcoanalytics")

    fake_gh.write_text(
        json.dumps(
            {
                "lake-of-rage#7": {
                    "headRefOid": "7777777lor",
                    "baseRefName": "main",
                    "additions": 3,
                    "deletions": 0,
                    "files": [{"path": "api/src/LOR-PR7.py"}],
                },
                "silphcoanalytics#7": {
                    "headRefOid": "7777777sil",
                    "baseRefName": "main",
                    "additions": 3,
                    "deletions": 0,
                    "files": [{"path": "frontend/src/x.tsx"}],
                },
            }
        ),
        encoding="utf-8",
    )

    status = tmp_path / "status"
    _write_status(status, "lake-of-rage", 7, "7777777")
    _write_status(status, "silphcoanalytics", 7, "7777777")

    repo_specs = {
        "lake-of-rage": builtin_spec("lake-of-rage", str(lake)),
        "silphcoanalytics": builtin_spec("silphcoanalytics", str(silph)),
    }

    plan = build_plan(repo_specs=repo_specs, status_dir=status, check_merges=False)

    excluded = {(p.repo, p.pr_number): p.reason for p in plan.excluded}
    for repo in ("lake-of-rage", "silphcoanalytics"):
        assert (repo, 7) not in excluded, f"{repo}#7 unexpectedly excluded: {excluded[(repo, 7)]}"

    by_repo = {b.repo: b for b in plan.batches}
    assert by_repo["silphcoanalytics"].deploy_unit == "frontend", (
        "silphcoanalytics#7 was profiled with another repo's files; got "
        f"deploy_unit={by_repo['silphcoanalytics'].deploy_unit!r}, reasons="
        f"{list(by_repo['silphcoanalytics'].reasons)}"
    )
    assert by_repo["lake-of-rage"].deploy_unit == "lor-api"
