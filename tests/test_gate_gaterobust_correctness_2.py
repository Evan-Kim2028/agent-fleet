"""merge_base_into must not swallow a conflicting merge of the base (correctness-2).

The recheck worktree is merged with the base so the PR's own tests run against
what the PR will actually merge into. When that merge conflicts, the worktree is
left with unresolved conflict markers and the run continues as if the base had
merged cleanly -- so the tests execute a tree that no real merge would produce.
A conflict has to be surfaced as a :class:`GateError` so ``run_gate_recheck``
refuses (``full gate required``) rather than reporting a verdict it never
established.
"""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime

import pytest

from agent_fleet.gate.gitops import (
    GateError,
    merge_base_into,
    prepare_worktree,
    resolve_diff_base,
)

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "gate-fixture"
version = "0.0.0"
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


@pytest.fixture
def conflicting_repo(tmp_path: Path) -> Path:
    """A repo where the base and the PR both change ``f.txt`` differently.

    main moves f.txt to ``3``; the PR branch moves it to ``2``. Merging main
    into the PR head is an unresolvable content conflict.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "f.txt").write_text("1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    # The PR branch: its own changed test, and a conflicting edit to f.txt.
    _git(root, "checkout", "-q", "-b", "pr")
    (root / "tests").mkdir()
    (root / "tests" / "test_new.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (root / "f.txt").write_text("2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr change")

    # main moves on, touching the same line differently.
    _git(root, "checkout", "-q", "main")
    (root / "f.txt").write_text("3\n", encoding="utf-8")
    _git(root, "commit", "-qam", "main change")
    return root


def test_conflicting_base_merge_raises(conflicting_repo: Path, tmp_path: Path) -> None:
    """A conflicting merge of the base must be reported, not silently ignored."""
    worktree = prepare_worktree(conflicting_repo, tmp_path / "recheck-wt", "pr")
    base = resolve_diff_base(conflicting_repo, "main")

    # Guard: the scenario really is an unresolvable content conflict, so this
    # test cannot pass vacuously if git's behaviour ever changes.
    probe = subprocess.run(
        ["git", "-C", str(worktree), "merge", "--no-edit", base],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert probe.returncode != 0, "fixture no longer produces a merge conflict"
    assert "CONFLICT" in (probe.stdout + probe.stderr)
    subprocess.run(
        ["git", "-C", str(worktree), "merge", "--abort"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert _git(worktree, "status", "--porcelain").strip() == ""

    # The defect: this returns None on a conflicting merge.
    with pytest.raises(GateError):
        merge_base_into(worktree, base)

    # And the worktree must not be left mid-merge for the recheck to test.
    assert "<<<<<<<" not in (worktree / "f.txt").read_text(encoding="utf-8")
    assert _git(worktree, "ls-files", "--unmerged") == ""
