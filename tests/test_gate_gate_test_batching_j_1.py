"""An ignored fixture under a directory named like a build artifact is invisible.

``_IGNORED_TREE_DIRS`` exists so that per-worktree noise — a virtualenv, a
``__pycache__``, a ``dist/`` left by a build — cannot split the cache key across
worktrees of one commit. Its members are matched on *any* path component, which
is right for the artifacts it was written for and wrong for everything else:
``env``, ``build``, ``dist`` and ``target`` are equally the natural names of a
directory holding test fixtures, and nothing in the code can tell a venv from
one.

The consequence is the one thing this cache must never do. ``git add -A`` never
sees an ignored file, so the tree hash is identical before and after an edit to
one; the ignored-file digest was added to close exactly that hole. When the
fixture's directory name happens to be in the skip list the digest collapses to
``""``, which is byte-identical to a repo that has no ignored files at all, so
the key does not move — and a cached verdict produced from the old bytes is
replayed as if it had just been measured.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping  # noqa: TC003 - annotation only
from typing import TYPE_CHECKING

import pytest

from agent_fleet.gate import pytest_runner as pr
from agent_fleet.gate.pytest_runner import (
    PytestResult,
    cache_key,
    cache_load,
    cache_store,
    ignored_files_digest,
    worktree_tree_hash,
)

if TYPE_CHECKING:
    from pathlib import Path

TEST_FILE = "tests/test_a.py"


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _fake_pytest(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace only the pytest launch; the cache's git half stays real."""
    launches: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(
        cmd: list[str],
        *,
        cwd: str | Path | None = None,
        capture_output: bool = False,
        text: bool | None = None,
        check: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if "pytest" not in cmd:
            return real_run(
                cmd,
                cwd=cwd,
                capture_output=capture_output,
                text=text,
                check=check,
                env=env,
                timeout=timeout,
            )
        launches.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "1 passed", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return launches


def _repo_with_ignored_fixture(root: Path, *, dir_name: str) -> Path:
    """A committed repo whose only ignored file is ``<dir_name>/secret.env``."""
    (root / "tests").mkdir(parents=True)
    (root / dir_name).mkdir()
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="cache-fixture"\nversion="0.0.0"\n',
        encoding="utf-8",
    )
    (root / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (root / "secret.env").write_text(f"{dir_name} is where the fixture lives\n", encoding="utf-8")
    (root / ".gitignore").write_text(f"{dir_name}/\n", encoding="utf-8")
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "gate@local", cwd=root)
    _git("config", "user.name", "gate", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    # The fixture itself, written after the commit and therefore never tracked.
    (root / dir_name / "secret.env").write_text("TOKEN = AAA\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("dir_name", ["env", "build", "dist", "target"])
def test_ignored_fixture_under_a_skipped_dir_name_misses_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dir_name: str
) -> None:
    """Editing the fixture must miss; a replayed verdict is the bug.

    Every name here is in ``_IGNORED_TREE_DIRS``, so the digest cannot see the
    edit, the key does not move, and ``cache_load`` hands back the verdict the
    first run measured against different bytes.
    """
    repo = _repo_with_ignored_fixture(tmp_path / "repo", dir_name=dir_name)
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch)

    pr.run_pytest(repo, [TEST_FILE], use_systemd=False, cache_dir=cache)
    (repo / dir_name / "secret.env").write_text("TOKEN = BBB\n", encoding="utf-8")
    pr.run_pytest(repo, [TEST_FILE], use_systemd=False, cache_dir=cache)

    assert len(launches) == 2, (
        f"an edit to a gitignored fixture under '{dir_name}/' must invalidate the cached "
        f"result; {len(launches)} launch(es) means a verdict measured against the old "
        "bytes was replayed"
    )


def test_a_changed_fixture_under_a_skipped_dir_name_yields_a_new_cache_key(
    tmp_path: Path,
) -> None:
    """The key must move when a gitignored fixture changes.

    ``git add -A`` cannot see an ignored file, so the tree hash is identical
    either side of the edit and the ignored-file digest is the only thing that
    can distinguish them. With every ignored file skipped the digest collapses
    to ``""`` — the documented value for a repo that has none — and the second
    edit is served the first one's cached key.
    """
    repo = _repo_with_ignored_fixture(tmp_path / "repo", dir_name="env")
    cache = tmp_path / "cache"

    tree_before = worktree_tree_hash(repo)
    digest_before = ignored_files_digest(repo)
    assert tree_before is not None
    assert digest_before is not None
    key_before = cache_key(tree_before, [TEST_FILE], package=".", ignored=digest_before)
    assert cache_load(cache, key_before, ttl_s=9_999) is None
    cache_store(cache, key_before, PytestResult(returncode=0, stdout="1 passed", stderr=""))

    (repo / "env" / "secret.env").write_text("TOKEN = BBB\n", encoding="utf-8")

    tree_after = worktree_tree_hash(repo)
    digest_after = ignored_files_digest(repo)
    assert tree_after is not None
    assert tree_after == tree_before, "the git tree cannot see a gitignored edit"
    assert digest_after is not None
    key_after = cache_key(tree_after, [TEST_FILE], package=".", ignored=digest_after)
    replayed = cache_load(cache, key_after, ttl_s=9_999)

    assert replayed is None, (
        "the fixture changed from TOKEN = AAA to TOKEN = BBB and the tree hash cannot "
        "see it, so the ignored-file digest must make the key move; a cached result "
        "coming back means a verdict measured against the old bytes was replayed"
    )
    assert key_after != key_before, (
        "an edit to the only ignored file in the repo must change its cache key"
    )


def test_a_fixture_under_an_ordinary_dir_name_does_invalidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: the identical setup under a name outside the skip list misses.

    Without this, the two tests above would pass for any reason at all — a
    broken tree hash, a cache directory that is never read — and prove nothing.
    """
    repo = _repo_with_ignored_fixture(tmp_path / "repo", dir_name="fixtures")
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch)

    assert ignored_files_digest(repo) != "", (
        "a fixture under 'fixtures/' is not a build artifact, so it must reach the digest"
    )
    pr.run_pytest(repo, [TEST_FILE], use_systemd=False, cache_dir=cache)
    (repo / "fixtures" / "secret.env").write_text("TOKEN = BBB\n", encoding="utf-8")
    pr.run_pytest(repo, [TEST_FILE], use_systemd=False, cache_dir=cache)

    assert len(launches) == 2, "the control must miss; if it does not, the tests above are void"
