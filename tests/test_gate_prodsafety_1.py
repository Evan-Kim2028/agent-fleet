"""Regression tests: every repo's PR head and file list must come from its own
checkout.

``build_plan`` constructs a single ``GitHubClient`` bound to one repo path
(``plan._first_repo_path`` picks the alphabetically-first repo that has a path),
and ``profile_approvals`` then calls ``client.pr_detail(pr_number)`` for every
repo through that same client.  ``gh`` resolves the repository from its cwd, so
every repo other than the bound one has its head SHA *and* its changed-file list
read from the wrong checkout.

The fake ``gh`` below answers per-cwd, exactly as the real one does, so the
planner reads the wrong repository whenever two repos are planned together.
Both fixtures approve PRs at the head their *own* repo reports, so any staleness
or lost risk flag is a defect rather than the fixture's doing.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_fleet.merge_plan.collect import GitHubClient, profile_approvals
from agent_fleet.merge_plan.config import builtin_spec
from agent_fleet.merge_plan.plan import build_plan
from agent_fleet.merge_plan.types import ApprovedPR, RepoSpec

# Hex-only, as the ``PREMERGE-APPROVED <sha>`` marker requires.
LOR_SHA = "a" * 40
SILPH_SHA = "b" * 40
#: The head both repos report for the PRs the shared-head fixture uses.
SHARED_SHA = "d" * 40

LOR_API_FILE = "api/lor_plain.py"
SILPH_API_FILE = "api/plain.py"
#: silphcoanalytics#8 touches a production migration, so rule 4 must isolate it.
MIGRATION = "migrations/0007_x.sql"


def _init_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _detail(head: str, path: str) -> dict:
    return {
        "headRefOid": head,
        "baseRefName": "main",
        "additions": 5,
        "deletions": 0,
        "files": [{"path": path}],
    }


def _install_fake_gh_per_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, per_repo: dict
) -> None:
    """A ``gh`` serving JSON keyed by the checkout it runs in.

    Mirrors the real tool: the repository is resolved from the current
    directory, so the same PR number means different things per checkout.
    """
    data_file = tmp_path / "gh_per_cwd.json"
    data_file.write_text(
        json.dumps({repo: {str(n): d for n, d in prs.items()} for repo, prs in per_repo.items()}),
        encoding="utf-8",
    )

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"data = json.load(open({str(data_file)!r}))\n"
        "repo = os.path.basename(os.getcwd())\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['pr', 'view']:\n"
        "    entry = data.get(repo, {}).get(args[2])\n"
        "    if entry is None:\n"
        "        sys.stderr.write('no such pr in ' + repo + '\\n')\n"
        "        sys.exit(1)\n"
        "    print(json.dumps(entry))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, per_repo: dict, approvals: dict
) -> dict:
    """Two checkouts, a fake per-cwd ``gh``, and a status dir of approvals."""
    # Keep the real lane registry out of the run.
    fleet_home = tmp_path / "fleet-home"
    (fleet_home / "lanes").mkdir(parents=True)
    monkeypatch.setenv("AGENT_FLEET_HOME", str(fleet_home))

    lor = tmp_path / "lor"
    silph = tmp_path / "silph"
    lor.mkdir()
    silph.mkdir()
    _init_repo(lor)
    _init_repo(silph)
    _install_fake_gh_per_cwd(tmp_path, monkeypatch, per_repo)

    # One file per PR: collect_from_status_dir takes only the first repo#N
    # reference in a file, so a file must name exactly one PR.
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    for filename, line in approvals.items():
        (status_dir / filename).write_text(line + "\n", encoding="utf-8")

    repo_specs: dict[str, RepoSpec] = {
        "lake-of-rage": builtin_spec("lake-of-rage", str(lor)),
        "silphcoanalytics": builtin_spec("silphcoanalytics", str(silph)),
    }
    return {"repo_specs": repo_specs, "status_dir": status_dir, "lor": lor, "silph": silph}


@pytest.fixture
def two_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Each repo's PRs sit at their own distinct head.

    No approval is genuinely stale, so reporting one stale is a defect.
    """
    return _env(
        tmp_path,
        monkeypatch,
        per_repo={
            "lor": {7: _detail(LOR_SHA, LOR_API_FILE)},
            "silph": {
                7: _detail(SILPH_SHA, SILPH_API_FILE),
                8: _detail(SILPH_SHA, MIGRATION),
            },
        },
        approvals={
            "lor-7.md": f"acme/lake-of-rage#7 PREMERGE-APPROVED {LOR_SHA}",
            "silph-7.md": f"acme/silphcoanalytics#7 PREMERGE-APPROVED {SILPH_SHA}",
            "silph-8.md": f"acme/silphcoanalytics#8 PREMERGE-APPROVED {SILPH_SHA}",
        },
    )


@pytest.fixture
def two_repos_shared_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Every PR passes its staleness check from either checkout.

    Only the changed-file list can still be read wrong, and that is what
    decides whether the production migration is isolated.
    """
    return _env(
        tmp_path,
        monkeypatch,
        per_repo={
            "lor": {
                7: _detail(SHARED_SHA, LOR_API_FILE),
                8: _detail(SHARED_SHA, LOR_API_FILE),
            },
            "silph": {
                7: _detail(SHARED_SHA, SILPH_API_FILE),
                8: _detail(SHARED_SHA, MIGRATION),
            },
        },
        approvals={
            "lor-7.md": f"acme/lake-of-rage#7 PREMERGE-APPROVED {SHARED_SHA}",
            "silph-7.md": f"acme/silphcoanalytics#7 PREMERGE-APPROVED {SHARED_SHA}",
            "silph-8.md": f"acme/silphcoanalytics#8 PREMERGE-APPROVED {SHARED_SHA}",
        },
    )


def _profile(two_repos: dict, approvals: list[ApprovedPR]):
    return profile_approvals(
        approvals,
        client=GitHubClient(cwd=two_repos["lor"]),
        repo_specs=two_repos["repo_specs"],
    )


def test_approved_prs_are_not_reported_stale(two_repos: dict) -> None:
    """Every approval matches its own repo's head, so none may be dropped."""
    approvals = [
        ApprovedPR(repo="lake-of-rage", pr_number=7, approved_sha=LOR_SHA, source="status_dir"),
        ApprovedPR(
            repo="silphcoanalytics", pr_number=7, approved_sha=SILPH_SHA, source="status_dir"
        ),
        ApprovedPR(
            repo="silphcoanalytics", pr_number=8, approved_sha=SILPH_SHA, source="status_dir"
        ),
    ]
    _batchable, _profiles, stale, unprofilable = _profile(two_repos, approvals)

    assert not stale, (
        "gate-approved PRs reported stale (head read from the wrong repo): "
        f"{[(p.repo, p.pr_number, p.reason) for p in stale]}"
    )
    assert not unprofilable, f"PRs unreadable: {[(p.repo, p.pr_number) for p in unprofilable]}"


def test_build_plan_ships_every_approved_pr(two_repos: dict) -> None:
    """The plan must contain all three approved PRs, none silently dropped."""
    plan = build_plan(
        repo_specs=two_repos["repo_specs"],
        status_dir=two_repos["status_dir"],
        check_merges=False,
    )

    stale = {p.repo for p in plan.excluded if p.stale}
    assert not stale, f"gate-approved PRs reported stale (wrong repo read): {stale}"

    planned = {(b.repo, pr.pr_number) for b in plan.batches for pr in b.prs}
    assert ("lake-of-rage", 7) in planned
    assert ("silphcoanalytics", 7) in planned
    assert ("silphcoanalytics", 8) in planned, "approved PR dropped from the plan"

    heads = {(b.repo, pr.pr_number): pr.head_sha for b in plan.batches for pr in b.prs}
    assert heads[("lake-of-rage", 7)] == LOR_SHA
    assert heads[("silphcoanalytics", 7)] == SILPH_SHA
    assert heads[("silphcoanalytics", 8)] == SILPH_SHA


def test_profiles_use_each_repos_own_file_list(two_repos_shared_head: dict) -> None:
    """The changed-file list must come from the PR's own repository."""
    approvals = [
        ApprovedPR(repo="lake-of-rage", pr_number=7, approved_sha=SHARED_SHA, source="status_dir"),
        ApprovedPR(
            repo="silphcoanalytics", pr_number=7, approved_sha=SHARED_SHA, source="status_dir"
        ),
        ApprovedPR(
            repo="silphcoanalytics", pr_number=8, approved_sha=SHARED_SHA, source="status_dir"
        ),
    ]
    _batchable, profiles, _stale, _unprofilable = _profile(two_repos_shared_head, approvals)

    assert profiles[("lake-of-rage", 7)].files == (LOR_API_FILE,)
    assert profiles[("silphcoanalytics", 7)].files == (SILPH_API_FILE,)
    assert profiles[("silphcoanalytics", 8)].files == (MIGRATION,)


def test_migration_in_second_repo_stays_isolated(two_repos_shared_head: dict) -> None:
    """Rule 4 must still isolate the production migration behind its own deploy.

    Read against the first repo, the migration's file list looks like an
    ordinary API change, so it would be packed with another PR behind a single
    shared deploy — destroying the blast-radius isolation.
    """
    plan = build_plan(
        repo_specs=two_repos_shared_head["repo_specs"],
        status_dir=two_repos_shared_head["status_dir"],
        check_merges=False,
    )

    migration_batches = [b for b in plan.batches if any(p.pr_number == 8 for p in b.prs)]
    assert len(migration_batches) == 1, "migration PR missing from the plan"
    assert migration_batches[0].repo == "silphcoanalytics"
    assert migration_batches[0].isolated_risk is True, "migration not isolated for risk"
    assert migration_batches[0].size == 1, "migration batched behind unrelated PRs"
