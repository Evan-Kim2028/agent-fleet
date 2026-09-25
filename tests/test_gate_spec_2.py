"""Gate check: the merge-compatibility probe must not demote every batch.

A lake-of-rage working copy is a normal clone; approved PR head commits live on
GitHub and are only present locally after a fetch.  The planner hands the
GitHub-reported head SHAs straight to the merge check, and nothing in
``agent_fleet/merge_plan/`` ever fetches them, so the check can only answer
"incompatible" and every multi-PR batch collapses into single-PR batches --
the exact outcome the feature exists to prevent.

Two approved PRs is enough to show it, and keeping the batch at two keeps the
merge sequence to a single merge so the control test isolates *materialisation*
rather than anything else about merge semantics.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent_fleet.merge_plan.batching import plan_batches
from agent_fleet.merge_plan.config import builtin_spec
from agent_fleet.merge_plan.profile import build_profile
from agent_fleet.merge_plan.types import ApprovedPR, RepoSpec

PR_NUMBERS = (1, 2)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return result.stdout.strip()


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _setup(tmp_path: Path) -> tuple[Path, dict[int, str]]:
    """An origin repo plus a clone taken *before* the PRs were pushed.

    Returns (local checkout, {pr_number: head sha}).  The head SHAs are what
    `gh pr view` reports; the clone does not have them, exactly like a working
    copy whose owner has not fetched since the PRs opened.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    # The operator's working copy: only main exists at clone time.
    checkout = tmp_path / "lake-of-rage"
    subprocess.run(
        ["git", "clone", "-q", f"file://{origin}", str(checkout)],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")

    # Then the PRs open upstream: disjoint files, one deploy unit, clean merges.
    shas: dict[int, str] = {}
    base = _git(seed, "rev-parse", "HEAD")
    for number in PR_NUMBERS:
        _git(seed, "checkout", "-q", base)
        target = seed / f"api/src/pr{number}.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# pr {number}\n", encoding="utf-8")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", f"pr {number}")
        _git(seed, "push", "-q", "origin", f"HEAD:refs/heads/pr/{number}")
        shas[number] = _git(seed, "rev-parse", "HEAD")
    return checkout, shas


def _plan(checkout: Path, shas: dict[int, str]) -> list[list[int]]:
    spec: RepoSpec = builtin_spec("lake-of-rage", path=str(checkout))
    prs = [
        ApprovedPR(repo="lake-of-rage", pr_number=n, approved_sha=shas[n], head_sha=shas[n])
        for n in PR_NUMBERS
    ]
    profiles = {
        ("lake-of-rage", pr.pr_number): build_profile(
            pr, files=[f"api/src/pr{pr.pr_number}.py"], repo_spec=spec
        )
        for pr in prs
    }
    batches = plan_batches(prs, profiles, repo_specs={"lake-of-rage": spec}, check_merges=True)
    return [[p.pr_number for p in batch.prs] for batch in batches]


def _heads_local(checkout: Path, shas: dict[int, str]) -> bool:
    return all(
        _run(checkout, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0
        for sha in shas.values()
    )


def test_unfetched_pr_heads_do_not_collapse_the_batch(tmp_path: Path) -> None:
    checkout, shas = _setup(tmp_path)

    # Precondition: these are the commits GitHub reports, and this working copy
    # does not have them -- the production state for a not-recently-fetched clone.
    assert not _heads_local(checkout, shas)

    # Disjoint, same deploy unit, two approved PRs: one deploy should cover both.
    assert _plan(checkout, shas) == [list(PR_NUMBERS)]


def test_control_same_plan_once_heads_are_fetched(tmp_path: Path) -> None:
    """Same input where the head commits *are* present.

    Proves the merge check itself accepts these PRs, so a failure in the test
    above is specifically about the commits never being materialised.
    """
    checkout, shas = _setup(tmp_path)
    _git(checkout, "fetch", "-q", "origin")
    assert _heads_local(checkout, shas)

    assert _plan(checkout, shas) == [list(PR_NUMBERS)]


@pytest.fixture(autouse=True)
def _require_git() -> None:
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git unavailable")
