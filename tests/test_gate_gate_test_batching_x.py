"""Regression test for the claim: the pytest result cache key is scoped to the
PACKAGE dir, not the WORKTREE root, so a real code change outside the package is
invisible to the key and a stale result is replayed.

The claim is verified here against the real code path the gate uses:
``GateTestRunner._run_package`` -> ``run_pytest(package.dir, ...)``, with
``package.dir`` derived by ``find_test_packages`` (pipeline.py:438).

Setup: a repo with ``pyproject.toml`` at the root AND at ``api/``. The test file
``api/tests/test_x.py`` imports ``rootcode`` from the REPO ROOT (i.e. code the
test actually executes, outside the ``api/`` package dir). ``worktree_tree_hash``
runs ``git -C <root> add -A .`` with cwd = the package dir, so paths under
``api/`` only. Editing ``rootcode.py`` at the repo root must change the key.

Expected on the fixed code: two pytest launches (the edit outside ``api/``
invalidates the key).
Buggy behaviour: one launch -- the cached rc=0 is replayed despite the change.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping  # noqa: TC003 - annotation only
from pathlib import Path  # noqa: TC003 - concrete paths built at runtime

import pytest

from agent_fleet.gate import pytest_runner as pr
from agent_fleet.gate.pipeline import GateTestRunner


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _fake_pytest(monkeypatch: pytest.MonkeyPatch, rc: int = 0) -> list[list[str]]:
    """Fake only the pytest launch; let git run for real against a real repo."""
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
        stdout = "1 passed" if rc == 0 else "FAILED api/tests/test_x.py::test_x\n1 failed"
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return launches


@pytest.fixture
def nested_repo(tmp_path: Path) -> Path:
    """A repo with a root package AND a nested ``api/`` package.

    ``api/tests/test_x.py`` imports ``rootcode`` from the repo root, so the test
    genuinely depends on code outside the ``api/`` package dir.
    """
    root = tmp_path / "repo"
    (root / "api" / "tests").mkdir(parents=True)

    # Root package + the shared module the test imports from the repo root.
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="root-pkg"\nversion="0.0.0"\n',
        encoding="utf-8",
    )
    (root / "rootcode.py").write_text("VALUE = 1\n", encoding="utf-8")

    # Nested package owning the test.
    (root / "api" / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="api-pkg"\nversion="0.0.0"\n',
        encoding="utf-8",
    )
    (root / "api" / "tests" / "test_x.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))\n"
        "import rootcode\n"
        "def test_x():\n"
        "    assert rootcode.VALUE == 1\n",
        encoding="utf-8",
    )

    _git("init", "-q", cwd=root)
    _git("config", "user.email", "gate@local", cwd=root)
    _git("config", "user.name", "gate", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def test_cache_key_must_cover_the_worktree_root_not_the_package_dir(
    nested_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code change OUTSIDE the nested package dir must invalidate the cache.

    The gate promises (docs/GATE.md, CHANGELOG, and worktree_tree_hash's own
    docstring) that the key covers "the git tree of the whole worktree". Here
    ``rootcode.py`` lives at the repo root but is imported by the test, so an
    edit to it is a real change to what the test observes -- yet it is invisible
    to a key computed from ``worktree_tree_hash(root / 'api')``.

    The runner is the real ``GateTestRunner`` (the production entry point used by
    GatePipeline), not a hand-rolled call, so this exercises the actual wiring.
    """
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rc=0)

    # Confirm the production path really does route the test to the nested
    # package (rel_dir 'api'), otherwise this test would not be about the bug.
    packages = GateTestRunner(
        root=nested_repo,
        memory="6G",
        timeout_s=60,
        use_systemd=False,
        cache_dir=cache,
        cache_ttl_s=3600,
    ).packages_for(["api/tests/test_x.py"])
    assert [p.rel_dir for p in packages] == ["api"], (
        "test fixture must produce a nested package so package.dir != worktree root"
    )

    runner = GateTestRunner(
        root=nested_repo,
        memory="6G",
        timeout_s=60,
        use_systemd=False,
        cache_dir=cache,
        cache_ttl_s=3600,
    )

    first = runner.run(["api/tests/test_x.py"])
    assert not first.tests_failed and not first.infra_error
    assert len(launches) == 1, "first run should launch pytest"

    # Edit code OUTSIDE the api/ package dir that the test actually imports.
    (nested_repo / "rootcode.py").write_text("VALUE = 2\n", encoding="utf-8")

    # The package dir tree hash is computed from the package dir, so it cannot
    # see an edit that lives outside it; the whole-worktree hash can.
    pkg_hash_before = pr.worktree_tree_hash(nested_repo / "api")
    root_hash_after = pr.worktree_tree_hash(nested_repo)
    assert pkg_hash_before is not None

    second = runner.run(["api/tests/test_x.py"])

    # The gate must re-run: the imported code changed. A single launch means the
    # cache replayed a stale result for code the fixer already changed.
    assert len(launches) == 2, (
        "a real change outside the package dir must miss the cache; "
        f"got {len(launches)} launch(es) (package_dir hash={pkg_hash_before}, "
        f"worktree_root hash={root_hash_after})"
    )
    assert not second.tests_failed and not second.infra_error


def test_worktree_tree_hash_of_package_dir_is_blind_to_root_edits(nested_repo: Path) -> None:
    """Directly pin the root cause: worktree_tree_hash(api) ignores root edits."""
    before = pr.worktree_tree_hash(nested_repo / "api")
    (nested_repo / "rootcode.py").write_text("VALUE = 99\n", encoding="utf-8")
    after = pr.worktree_tree_hash(nested_repo / "api")

    # worktree_tree_hash's docstring promises "the WHOLE worktree". Hashing a
    # subdirectory cannot honour that; this asserts the correct behaviour so it
    # fails on the current head (identical hashes) and passes once the key is
    # rooted at the worktree.
    assert before != after, (
        "worktree_tree_hash on a subdirectory cannot see edits made outside it; "
        "the cache key must be computed from the worktree root, not the package dir"
    )
