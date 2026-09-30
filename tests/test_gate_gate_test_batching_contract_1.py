"""A failed `git ls-files` must not key a run like a repo with no ignored files.

The cache key is (worktree tree, package, ignored-file digest, test list). The
tree hash cannot see a gitignored file -- `git add -A` skips them -- so the
ignored digest is the ONLY part of the key that moves when a test's ignored
input changes. `ignored_files_digest` answers `""` both for a repo that has no
ignored files and for one whose listing *failed*, so a transient git failure
silently collapses the key to the no-ignored-files value and a later run replays
a verdict produced from different gitignored bytes: the exact stale evidence the
three-part key exists to prevent.

The failure is realistic rather than theoretical: `worktree_tree_hash` and
`ignored_files_digest` make independent git calls with independent timeouts, so
a hung or overloaded git can time out the 120s `ls-files` while `add -A` and
`write-tree` succeed. A run must degrade to "uncached" (as `worktree_tree_hash`
returns None for a git that cannot write a tree), never to a confident key.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping  # noqa: TC003 - annotation only
from typing import TYPE_CHECKING

import pytest

from agent_fleet.gate import pytest_runner as pr
from agent_fleet.gate.pytest_runner import (
    cache_key,
    ignored_files_digest,
    run_pytest,
    worktree_tree_hash,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init(root: Path) -> None:
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "gate@local", cwd=root)
    _git("config", "user.name", "gate", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)


@pytest.fixture
def repo_with_ignored_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """A committed repo whose test input is a gitignored file."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname="ignored-fixture"\nversion="0.0.0"\n', encoding="utf-8"
    )
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (root / ".gitignore").write_text("secrets/\n", encoding="utf-8")
    (root / "secrets").mkdir()
    env_file = root / "secrets" / ".env"
    env_file.write_text("TOKEN=original\n", encoding="utf-8")
    _init(root)
    return root, env_file


def _break_only_ls_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ignored-file listing fail while every other git call still works.

    git keeps running for real otherwise, so the tree hash stays a genuine hash
    of a genuine worktree. This is the selective failure the claim describes:
    `worktree_tree_hash` and `ignored_files_digest` issue independent git calls.
    """
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
        if "ls-files" in cmd:
            raise subprocess.TimeoutExpired(cmd, timeout or 120)
        return real_run(
            cmd,
            cwd=cwd,
            capture_output=capture_output,
            text=text,
            check=check,
            env=env,
            timeout=timeout,
        )

    monkeypatch.setattr(pr.subprocess, "run", fake_run)


def test_a_failed_listing_must_not_be_indistinguishable_from_no_ignored_files(
    repo_with_ignored_fixture: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest of a failure must not equal the digest of a clean repo."""
    root, _env_file = repo_with_ignored_fixture

    healthy = ignored_files_digest(root)
    assert healthy != "", "fixture repo really does hold a gitignored file"

    # A repo with no ignored files at all: the value a failure must not collide with.
    clean = root.parent / "clean"
    clean.mkdir()
    (clean / "pyproject.toml").write_text(
        '[project]\nname="clean"\nversion="0.0.0"\n', encoding="utf-8"
    )
    _init(clean)
    assert ignored_files_digest(clean) == ""

    _break_only_ls_files(monkeypatch)
    degraded = ignored_files_digest(root)

    assert degraded != "", (
        "a failed `git ls-files` produced the same empty digest as a repo with no "
        "ignored files, so the ignored input the digest exists to cover became "
        "invisible to the cache key"
    )


def test_a_failed_listing_must_not_replay_a_stale_verdict(
    repo_with_ignored_fixture: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: edit the ignored input, and the second run must not replay."""
    root, env_file = repo_with_ignored_fixture
    # The cache lives OUTSIDE the worktree, as the real default does; a cache dir
    # inside the repo would itself change the tree hash and mask the defect.
    cache = tmp_path / "cache"
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
        if "pytest" in cmd:
            launches.append(list(cmd))
            return subprocess.CompletedProcess(
                cmd, 1, "FAILED tests/test_a.py::test_a\n1 failed", ""
            )
        if "ls-files" in cmd:
            raise subprocess.TimeoutExpired(cmd, timeout or 120)
        return real_run(
            cmd,
            cwd=cwd,
            capture_output=capture_output,
            text=text,
            check=check,
            env=env,
            timeout=timeout,
        )

    monkeypatch.setattr(pr.subprocess, "run", fake_run)

    first = run_pytest(
        root, ["tests/test_a.py"], use_systemd=False, cache_dir=cache, cache_ttl_s=9999
    )
    assert first.returncode == 1
    assert len(launches) == 1

    # The bytes a test reads changed; the git tree cannot see that.
    env_file.write_text("TOKEN=DIFFERENT-BYTES\n", encoding="utf-8")
    assert worktree_tree_hash(root) is not None, "git itself still works"

    second = run_pytest(
        root, ["tests/test_a.py"], use_systemd=False, cache_dir=cache, cache_ttl_s=9999
    )

    assert len(launches) == 2, (
        f"the second run was served from cache: with the listing broken the digest "
        f"collapsed to '' and run #1's rc={first.returncode} was replayed after the "
        f"ignored input changed (run #2 rc={second.returncode})"
    )


def test_the_key_must_not_be_the_key_of_a_repo_with_no_ignored_files(
    repo_with_ignored_fixture: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the key itself: a failed listing must not mint the no-ignored-files key.

    `ignored=""` is precisely what a repo holding no ignored files produces, so
    it is the value a broken listing must never be indistinguishable from. An
    entry stored under it is served to any later run whose tree matches, whatever
    its ignored files now hold.
    """
    root, _env_file = repo_with_ignored_fixture
    tree = worktree_tree_hash(root)
    assert tree is not None
    tests = ["tests/test_a.py"]

    no_ignored_files_key = cache_key(tree, tests, package=".", ignored="")
    healthy_key = cache_key(tree, tests, package=".", ignored=ignored_files_digest(root))
    assert no_ignored_files_key != healthy_key, "fixture sanity: the two really differ"

    _break_only_ls_files(monkeypatch)
    degraded_key = cache_key(tree, tests, package=".", ignored=ignored_files_digest(root))

    assert degraded_key != no_ignored_files_key, (
        "a failed `git ls-files` minted the same cache key as a repo with no "
        "ignored files, so a transient git failure lets a stored verdict be "
        "replayed after gitignored test input changed"
    )
