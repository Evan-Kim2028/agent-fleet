"""Gate check: a folded merge must not feed a tree OID back to ``merge-tree``.

Claim under test (``agent_fleet/merge_plan/batching.py``,
``_merge_tree_compatible``): the loop reassigns ``base`` to the first token of
``git merge-tree --write-tree`` output, which is a *tree* oid.  Modern git
(>= 2.38) requires a commit as the merge base, so the second and later folds
fail, ``_merge_tree_compatible`` returns False, and every batch of 3+ PRs is
demoted to single-PR batches.

The host git may predate 2.38 (in which case the code takes the scratch-worktree
fallback and the defect is invisible), so this test puts a shim on PATH that
reports 2.54 and emulates ``merge-tree --write-tree`` with the real git:

* a base argument that does not dereference to a commit is rejected, exactly
  as git >= 2.38 does ("expected commit type, but the object dereferences to
  tree type");
* two commits are really merged (scratch worktree, ``--no-commit``,
  ``write-tree``) and the resulting *tree* oid is printed, so a correct
  implementation that folds commits keeps working and conflicts still fail.

Nothing about the verdict is hardcoded: the merges are performed for real, only
git's argument-type contract for the base is modelled.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.merge_plan.batching import merge_compatible, plan_batches
from agent_fleet.merge_plan.config import builtin_spec
from agent_fleet.merge_plan.profile import build_profile
from agent_fleet.merge_plan.types import ApprovedPR

if TYPE_CHECKING:
    from pathlib import Path

#: A `git` wrapper that advertises >= 2.38 and emulates `merge-tree --write-tree`
#: on top of the real binary, so the tested code path is the one the claim is about.
GIT_SHIM = """#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
import tempfile

REAL = os.environ["GATE_REAL_GIT"]
REPO = os.getcwd()
ARGS = sys.argv[1:]


def git(*a, cwd=None):
    return subprocess.run([REAL, *a], cwd=cwd or REPO, capture_output=True, text=True)


if ARGS[:1] == ["--version"]:
    print("git version 2.54.0")
    sys.exit(0)

if ARGS[:2] == ["merge-tree", "--write-tree"]:
    base, other = ARGS[2], ARGS[3]
    kind = git("cat-file", "-t", base)
    obj_type = kind.stdout.strip() if kind.returncode == 0 else "unknown"
    if obj_type != "commit":
        # git >= 2.38 only merges commits, never bare trees.
        sys.stderr.write(
            "error: %s: expected commit type, but the object dereferences to %s type\\n"
            % (base, obj_type)
        )
        sys.stderr.write("merge-tree: %s - not something we can merge\\n" % base)
        sys.exit(1)
    tmp = tempfile.mkdtemp(prefix="gate-mergetree-")
    worktree = os.path.join(tmp, "wt")
    try:
        if git("worktree", "add", "--detach", "-q", worktree, base).returncode != 0:
            sys.exit(1)
        merged = git("merge", "--no-commit", "--no-ff", "-q", other, cwd=worktree)
        if merged.returncode != 0:
            # A real conflict: the batch must not be treated as compatible.
            sys.exit(1)
        tree = git("write-tree", cwd=worktree)
        if tree.returncode != 0:
            sys.exit(1)
        print(tree.stdout.strip())
        sys.exit(0)
    finally:
        git("worktree", "remove", "--force", worktree)
        shutil.rmtree(tmp, ignore_errors=True)

sys.exit(subprocess.run([REAL, *ARGS], cwd=REPO).returncode)
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=120
    )
    return result.stdout.strip()


@pytest.fixture
def modern_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put a git >= 2.38 on PATH for the duration of the test."""
    real_git = shutil.which("git")
    assert real_git, "no git on PATH"
    bindir = tmp_path / "shimbin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(GIT_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("GATE_REAL_GIT", real_git)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return bindir


@pytest.fixture
def three_disjoint_prs(tmp_path: Path) -> tuple[Path, list[ApprovedPR]]:
    """A repo with three approved PRs, each touching a different api/ file."""
    return _make_prs(tmp_path, conflict_between=())


@pytest.fixture
def conflicting_prs(tmp_path: Path) -> tuple[Path, list[ApprovedPR]]:
    """The same three PRs, but 1 and 2 both rewrite api/src/shared.py."""
    return _make_prs(tmp_path, conflict_between=(1, 2))


def _make_prs(
    tmp_path: Path, *, conflict_between: tuple[int, ...]
) -> tuple[Path, list[ApprovedPR]]:
    repo = tmp_path / "lake-of-rage"
    (repo / "api" / "src").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "api" / "src" / "f0.py").write_text("base\n", encoding="utf-8")
    (repo / "api" / "src" / "shared.py").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")

    prs: list[ApprovedPR] = []
    for number in (1, 2, 3):
        _git(repo, "checkout", "-q", "-b", f"pr-{number}", "main")
        if number in conflict_between:
            (repo / "api" / "src" / "shared.py").write_text(f"from pr {number}\n", encoding="utf-8")
        else:
            (repo / "api" / "src" / f"f{number}.py").write_text(
                f"# pr {number}\n", encoding="utf-8"
            )
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"pr {number}")
        sha = _git(repo, "rev-parse", "HEAD")
        prs.append(
            ApprovedPR(repo="lake-of-rage", pr_number=number, approved_sha=sha, head_sha=sha)
        )
    _git(repo, "checkout", "-q", "main")
    return repo, prs


def test_three_disjoint_prs_stay_in_one_batch(
    modern_git: Path,  # noqa: ARG001 - fixture used for its side effect
    three_disjoint_prs: tuple[Path, list[ApprovedPR]],
) -> None:
    """Three cleanly-merging PRs of one deploy unit must plan as one batch.

    ``check_merges`` is on and ``repo_spec.path`` points at a real checkout, so
    the merge check really runs.  The fold is what breaks: only batches of 3+
    merge more than one SHA, so a batch of 2 cannot detect the defect.
    """
    repo, prs = three_disjoint_prs
    spec = builtin_spec("lake-of-rage", path=str(repo))
    profiles = {
        (pr.repo, pr.pr_number): build_profile(
            pr, files=[f"api/src/f{pr.pr_number}.py"], repo_spec=spec
        )
        for pr in prs
    }

    # The shim reports 2.54, so the merge-tree path (not the fallback) is used.
    assert shutil.which("git")
    batches = plan_batches(prs, profiles, repo_specs={"lake-of-rage": spec}, check_merges=True)

    planned = [[p.pr_number for p in batch.prs] for batch in batches]
    assert planned == [[1, 2, 3]], (
        f"three disjoint, cleanly-merging PRs were split into {planned}; "
        "the folded merge check is rejecting a tree oid as a merge base"
    )


def test_merge_compatible_true_for_three_disjoint_shas(
    modern_git: Path,  # noqa: ARG001 - fixture used for its side effect
    three_disjoint_prs: tuple[Path, list[ApprovedPR]],
) -> None:
    """The same fold, called directly: three disjoint commits merge cleanly."""
    repo, prs = three_disjoint_prs
    shas = [pr.head_sha for pr in prs]
    assert merge_compatible(repo_path=repo, shas=shas, check=True) is True


def test_conflicting_prs_are_still_demoted(
    modern_git: Path,  # noqa: ARG001 - fixture used for its side effect
    conflicting_prs: tuple[Path, list[ApprovedPR]],
) -> None:
    """Control: the merge check must still reject PRs that truly conflict.

    Guards against "fixing" the fold by weakening the check — a passing
    assertion here means the failure above is a real false negative.
    """
    repo, prs = conflicting_prs
    spec = builtin_spec("lake-of-rage", path=str(repo))
    profiles = {
        (pr.repo, pr.pr_number): build_profile(
            pr, files=[f"api/src/f{pr.pr_number}.py"], repo_spec=spec
        )
        for pr in prs
    }
    assert (
        merge_compatible(repo_path=repo, shas=[prs[0].head_sha, prs[1].head_sha], check=True)
        is False
    )

    batches = plan_batches(prs, profiles, repo_specs={"lake-of-rage": spec}, check_merges=True)
    planned = [[p.pr_number for p in batch.prs] for batch in batches]
    assert planned == [[1], [2], [3]]
