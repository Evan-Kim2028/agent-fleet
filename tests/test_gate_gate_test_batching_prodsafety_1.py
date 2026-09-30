"""The ignored-file digest's `max_files` cap must not hide later ignored files.

The cache's whole promise is that a result is replayed only when the bytes the
test read are unchanged, and the ignored digest is the only part of the key that
can see a gitignored file. `ignored_files_digest` stops folding entries once it
has seen `max_files` (20,000) of them, so an ignored file that sorts past that
cutoff is invisible: editing it leaves the digest identical, and a stored
verdict is replayed for a tree whose ignored input changed. That is the stale
hit both the function's own docstring and the CHANGELOG state can never happen
("adding, editing or removing an ignored file all miss").

25,000 ignored files in a plain ignored directory is not exotic: a build log
directory or a data drop is exactly that shape, and none of them is a dependency
tree the `_IGNORED_TREE_DIRS` skip already covers.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.gate.pytest_runner import (
    PytestResult,
    cache_key,
    cache_load,
    cache_store,
    ignored_files_digest,
)

if TYPE_CHECKING:
    from pathlib import Path

#: Above the hard-coded `max_files=20_000` default of `ignored_files_digest`, but
#: small enough to keep the test quick.
IGNORED_FILE_COUNT = 21_000


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture(scope="module")
def many_ignored_files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A repo with more gitignored files than the digest's cap can fold in."""
    root = tmp_path_factory.mktemp("cap") / "repo"
    logs = root / "logs"  # an ordinary ignored dir, not a dependency tree
    logs.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname="cap-fixture"\nversion="0.0.0"\n', encoding="utf-8"
    )
    # `logs/` is not in _IGNORED_TREE_DIRS, so nothing here is skipped by that rule.
    (root / ".gitignore").write_text("logs/\n", encoding="utf-8")
    for i in range(IGNORED_FILE_COUNT):
        (logs / f"f{i:06d}.log").write_text(f"seed {i}\n", encoding="utf-8")
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "gate@local", cwd=root)
    _git("config", "user.name", "gate", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root, logs


def test_editing_an_ignored_file_past_the_cap_must_change_the_digest(
    many_ignored_files: tuple[Path, Path],
) -> None:
    """The last-listed ignored file is the cheapest probe of the cutoff."""
    root, logs = many_ignored_files
    victim = logs / f"f{IGNORED_FILE_COUNT - 1:06d}.log"

    before = ignored_files_digest(root)
    assert before != "", "fixture sanity: the repo really does hold ignored files"

    victim.write_text("EDITED - different bytes\n", encoding="utf-8")
    after = ignored_files_digest(root)

    assert after != before, (
        f"editing {victim.name} left the ignored-file digest unchanged: the digest "
        f"stops after 20,000 paths, so any ignored file beyond that cutoff is "
        f"invisible and a verdict produced from the old bytes is replayed"
    )


def test_a_changed_ignored_file_past_the_cap_must_miss_the_cache(
    many_ignored_files: tuple[Path, Path], tmp_path: Path
) -> None:
    """End to end: a stored PASS must not be served after the ignored input changed."""
    root, logs = many_ignored_files
    victim = logs / f"f{IGNORED_FILE_COUNT - 1:06d}.log"
    cache = tmp_path / "cache"
    tests = ["tests/test_a.py"]
    tree = "treehash"

    before_key = cache_key(tree, tests, package=".", ignored=ignored_files_digest(root))
    assert cache_store(cache, before_key, PytestResult(returncode=0, stdout="1 passed", stderr=""))

    victim.write_text("EDITED - the test now reads different bytes\n", encoding="utf-8")
    after_key = cache_key(tree, tests, package=".", ignored=ignored_files_digest(root))

    assert after_key != before_key, (
        "the cache key did not move for an ignored-file edit past the digest cap"
    )
    replayed = cache_load(cache, after_key, ttl_s=9999)
    assert replayed is None, (
        f"a PASS produced before the edit was replayed for a tree whose ignored "
        f"input changed: {replayed!r}"
    )
