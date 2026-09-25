"""Contract 1: each PR is read from *its own* repository's checkout.

``build_plan`` accepts a set of repos and resolves every ``gh pr view <n>``
through a single ``GitHubClient``.  Because ``gh`` resolves the repository from
its working directory, that client must be scoped to the repo the PR belongs
to — otherwise every repo after the first is read against the wrong GitHub
repository and its approvals are silently misreported (stale) or dropped
(unreadable).

The fake ``gh`` installed here is *cwd-aware*: it resolves the repository the
way real ``gh`` does (``git remote get-url origin`` in the process's working
directory) and only serves PRs belonging to that repository.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.merge_plan import build_plan, resolve_repo_specs

LOR_HEAD = "aaaa1b2c" + "0" * 32
SILPH_HEAD = "bbbb2c3d4" + "1" * 32


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def _make_checkout(root: Path, name: str, remote: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "remote", "add", "origin", remote)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def cwd_aware_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake ``gh`` that only knows the PRs of its own repository.

    Mirrors real ``gh``: the repository is derived from the ``origin`` remote of
    the process's working directory, so a client pointed at the wrong checkout
    gets "no such pull request" instead of another repo's PR.
    """
    data_file = tmp_path / "gh_data.json"
    data_file.write_text(
        json.dumps(
            {
                "acme/lake-of-rage": {
                    "12": {
                        "headRefOid": LOR_HEAD,
                        "baseRefName": "main",
                        "additions": 5,
                        "deletions": 1,
                        "files": [{"path": "api/lor/thing.py"}],
                    }
                },
                "acme/silphcoanalytics": {
                    "12": {
                        "headRefOid": SILPH_HEAD,
                        "baseRefName": "main",
                        "additions": 3,
                        "deletions": 2,
                        "files": [{"path": "api/silph/thing.py"}],
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
        "import json, subprocess, sys\n"
        f"data = json.load(open({str(data_file)!r}))\n"
        "args = sys.argv[1:]\n"
        "if args[:2] != ['pr', 'view']:\n"
        "    sys.exit(1)\n"
        "remote = subprocess.run(\n"
        "    ['git', 'remote', 'get-url', 'origin'],\n"
        "    capture_output=True, text=True, check=False, timeout=30,\n"
        ")\n"
        "if remote.returncode != 0:\n"
        "    sys.stderr.write('not a git repository\\n')\n"
        "    sys.exit(1)\n"
        "url = remote.stdout.strip().removesuffix('.git')\n"
        "if 'github.com/' in url:\n"
        "    repo = url.split('github.com/')[-1]\n"
        "elif ':' in url:\n"
        "    repo = url.rsplit(':', 1)[-1]\n"
        "else:\n"
        "    repo = url\n"
        "entry = data.get(repo, {}).get(args[2])\n"
        "if entry is None:\n"
        "    sys.stderr.write('no such pull request\\n')\n"
        "    sys.exit(1)\n"
        "print(json.dumps(entry))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_each_pr_is_read_from_its_own_repository(tmp_path: Path, cwd_aware_gh: None) -> None:
    """Two checkouts, both with an approved PR #12 — both must survive planning.

    Both repos deliberately use the *same* PR number and different head SHAs,
    which is exactly the case a shared, first-repo-pinned client gets wrong.
    """
    lor = _make_checkout(tmp_path / "checkouts", "lake-of-rage", "git@github.com:acme/lake-of-rage.git")
    silph = _make_checkout(
        tmp_path / "checkouts", "silphcoanalytics", "git@github.com:acme/silphcoanalytics.git"
    )

    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "lor.txt").write_text(
        f"PREMERGE-APPROVED {LOR_HEAD}\nacme/lake-of-rage#12\n", encoding="utf-8"
    )
    (status_dir / "silph.txt").write_text(
        f"PREMERGE-APPROVED {SILPH_HEAD}\nacme/silphcoanalytics#12\n", encoding="utf-8"
    )

    repo_specs = resolve_repo_specs([str(lor), str(silph)])
    assert set(repo_specs) == {"lake-of-rage", "silphcoanalytics"}

    plan = build_plan(
        repo_specs=repo_specs,
        status_dir=status_dir,
        check_merges=False,
    )

    planned = {
        (batch.repo, pr.pr_number, pr.head_sha) for batch in plan.batches for pr in batch.prs
    }
    assert planned == {
        ("lake-of-rage", 12, LOR_HEAD),
        ("silphcoanalytics", 12, SILPH_HEAD),
    }, (
        "each approved PR must be profiled from its own repository's checkout; "
        f"got planned={sorted(planned)} "
        f"excluded={[(e.repo, e.pr_number, e.reason) for e in plan.excluded]}"
    )
    assert not plan.excluded, [(e.repo, e.pr_number, e.reason) for e in plan.excluded]
