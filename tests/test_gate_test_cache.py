"""Tests for the gate's pytest result cache.

The cache exists because a gate run executes the same test set many times — once
per verified claim, then every fix round. The safety property is the whole point:
a result is replayed ONLY when the worktree is byte-for-byte the same tree the
run saw, uncommitted edits and untracked files included. Every test here is
written to fail if that weakens into "reuse the last result".
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping  # noqa: TC003 - annotation only
from pathlib import Path  # noqa: TC003 - concrete paths are built at runtime

import pytest

from agent_fleet.gate import pytest_runner as pr
from agent_fleet.gate.pipeline import GateTestRunner
from agent_fleet.gate.pytest_runner import (
    DEFAULT_CACHE_DIR,
    PytestResult,
    cache_key,
    cache_load,
    cache_store,
    prune_cache,
    worktree_tree_hash,
)


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _fake_pytest(monkeypatch: pytest.MonkeyPatch, rcs: list[int] | int = 0) -> list[list[str]]:
    """Fake only the *pytest launch*, counting invocations.

    The cache's git half must stay real. Faking the whole subprocess layer would
    also fake the tree hash, and a cache that trusts its own faked key proves
    nothing — so git keeps running for real against a real repo, and only the
    pytest process (the expensive thing the cache exists to avoid) is replaced.
    """
    launches: list[list[str]] = []
    codes = [rcs] if isinstance(rcs, int) else list(rcs)
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
        code = codes.pop(0) if len(codes) > 1 else codes[0]
        if code == 1:
            stdout = "FAILED tests/test_a.py::test_a\n1 failed"
        elif code >= 2:
            stdout = "ERROR collecting tests/test_a.py"
        else:
            stdout = "1 passed"
        return subprocess.CompletedProcess(cmd, code, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return launches


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed git repo with a package layout the runner understands."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="cache-fixture"\nversion="0.0.0"\n',
        encoding="utf-8",
    )
    (root / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "gate@local", cwd=root)
    _git("config", "user.name", "gate", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


# ---------------------------------------------------------------------------
# worktree_tree_hash — the key must cover the WHOLE worktree
# ---------------------------------------------------------------------------


def test_tree_hash_is_stable_when_nothing_changes(repo: Path) -> None:
    assert worktree_tree_hash(repo) == worktree_tree_hash(repo)


def test_tree_hash_tracks_a_committed_change(repo: Path) -> None:
    before = worktree_tree_hash(repo)
    (repo / "code.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "change", cwd=repo)
    assert worktree_tree_hash(repo) != before


def test_tree_hash_tracks_an_uncommitted_edit(repo: Path) -> None:
    """A dirty worktree is the normal state here: the fixer edits without committing."""
    before = worktree_tree_hash(repo)
    (repo / "code.py").write_text("VALUE = 99\n", encoding="utf-8")
    assert worktree_tree_hash(repo) != before


def test_tree_hash_tracks_an_untracked_file(repo: Path) -> None:
    """A verifier's brand-new test is untracked; ignoring it would replay a stale verdict."""
    before = worktree_tree_hash(repo)
    (repo / "tests" / "test_gate_new.py").write_text("x = 1\n", encoding="utf-8")
    assert worktree_tree_hash(repo) != before


def test_tree_hash_tracks_a_deletion(repo: Path) -> None:
    before = worktree_tree_hash(repo)
    (repo / "code.py").unlink()
    assert worktree_tree_hash(repo) != before


def test_tree_hash_does_not_disturb_the_real_index(repo: Path) -> None:
    """The gate shares this worktree with running agents; the index must survive."""
    (repo / "code.py").write_text("VALUE = 3\n", encoding="utf-8")
    before = _git("diff", "--cached", "--name-only", cwd=repo)
    worktree_tree_hash(repo)
    assert _git("diff", "--cached", "--name-only", cwd=repo) == before


def test_tree_hash_is_none_outside_a_git_repo(tmp_path: Path) -> None:
    """A non-repo must degrade to "run uncached", never raise or return a bogus key."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert worktree_tree_hash(plain) is None


# ---------------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------------


def test_cache_key_ignores_test_order() -> None:
    a = cache_key("tree", ["t2.py", "t1.py"], package=".")
    b = cache_key("tree", ["t1.py", "t2.py"], package=".")
    assert a == b


def test_cache_key_changes_with_the_test_list() -> None:
    assert cache_key("tree", ["t1.py"], package=".") != cache_key(
        "tree", ["t1.py", "t2.py"], package="."
    )


def test_cache_key_changes_with_the_tree() -> None:
    assert cache_key("t1", ["t1.py"], package=".") != cache_key("t2", ["t1.py"], package=".")


def test_cache_key_changes_with_the_package() -> None:
    """The same relative path is a different file under a different package root."""
    assert cache_key("t", ["tests/test_a.py"], package=".") != cache_key(
        "t", ["tests/test_a.py"], package="api"
    )


# ---------------------------------------------------------------------------
# store / load
# ---------------------------------------------------------------------------


def test_round_trip_preserves_result(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    result = PytestResult(returncode=1, stdout="FAILED a::t\n1 failed", stderr="warn")
    assert cache_store(cache, "k", result)
    assert cache_load(cache, "k", ttl_s=3600) == result


def test_miss_when_absent(tmp_path: Path) -> None:
    assert cache_load(tmp_path / "nope", "k", ttl_s=3600) is None


def test_only_real_results_are_cached(tmp_path: Path) -> None:
    """rc >= 2 is a collection/infra failure; replaying it would fake a verdict."""
    cache = tmp_path / "cache"
    for rc in (2, 3, 4, 5):
        assert not cache_store(
            cache, f"k{rc}", PytestResult(returncode=rc, stdout="boom", stderr="")
        )
    for rc in (0, 1):
        assert cache_store(cache, f"k{rc}", PytestResult(returncode=rc, stdout="ok", stderr=""))
    assert cache_load(cache, "k2", ttl_s=3600) is None


def test_expired_entry_is_a_miss_and_not_returned(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache_store(cache, "k", PytestResult(returncode=0, stdout="ok", stderr=""))
    stale = time.time() - 7200
    os.utime(cache / "k.json", (stale, stale))
    assert cache_load(cache, "k", ttl_s=3600) is None


def test_entry_within_ttl_is_returned(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache_store(cache, "k", PytestResult(returncode=0, stdout="ok", stderr=""))
    assert cache_load(cache, "k", ttl_s=3600) is not None


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "k.json").write_text("{not json", encoding="utf-8")
    assert cache_load(cache, "k", ttl_s=3600) is None


def test_entry_from_another_format_version_is_dropped(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "k.json").write_text('{"v": 999, "rc": 0, "stdout": "old"}', encoding="utf-8")
    assert cache_load(cache, "k", ttl_s=3600) is None


def test_prune_removes_only_expired_entries(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache_store(cache, "fresh", PytestResult(returncode=0, stdout="ok", stderr=""))
    cache_store(cache, "stale", PytestResult(returncode=0, stdout="ok", stderr=""))
    os.utime(cache / "stale.json", (0, 0))
    assert prune_cache(cache, 3600) == 1
    assert cache_load(cache, "fresh", ttl_s=3600) is not None
    assert cache_load(cache, "stale", ttl_s=3600) is None


def test_prune_on_a_missing_dir_is_a_noop(tmp_path: Path) -> None:
    assert prune_cache(tmp_path / "absent", 3600) == 0


# ---------------------------------------------------------------------------
# run_pytest integration
# ---------------------------------------------------------------------------


def test_run_pytest_serves_a_repeat_run_from_cache(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=1)
    first = pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    second = pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert len(launches) == 1, "second identical run should not have launched pytest"
    assert second.returncode == first.returncode == 1
    assert second.stdout == first.stdout


def test_any_file_change_misses_the_cache(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this cache could plausibly ship: a stale hit after an edit."""
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    (repo / "code.py").write_text("VALUE = 42\n", encoding="utf-8")
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert len(launches) == 2, "an uncommitted edit must invalidate the cached result"


def test_an_untracked_file_misses_the_cache(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verifier's new test file is untracked; keying on HEAD alone would replay."""
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    (repo / "tests" / "test_gate_fresh.py").write_text("x = 1\n", encoding="utf-8")
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert len(launches) == 2


def test_unchanged_worktree_hits_cache_across_a_different_test_list(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)
    (repo / "tests" / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    pr.run_pytest(repo, ["tests/test_a.py", "tests/test_b.py"], use_systemd=False, cache_dir=cache)
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert len(launches) == 2, "only the new test list should have been run"


def test_argument_order_does_not_defeat_the_cache(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)
    (repo / "tests" / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    pr.run_pytest(repo, ["tests/test_a.py", "tests/test_b.py"], use_systemd=False, cache_dir=cache)
    pr.run_pytest(repo, ["tests/test_b.py", "tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert len(launches) == 1


def test_infra_error_is_not_replayed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    _fake_pytest(monkeypatch, rcs=[2, 0])
    first = pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    second = pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert first.returncode == 2
    assert second.returncode == 0, "a cached infra error must be re-run, not replayed"


def test_no_cache_dir_means_no_caching(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launches = _fake_pytest(monkeypatch, rcs=0)
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False)
    pr.run_pytest(repo, ["tests/test_a.py"], use_systemd=False)
    assert len(launches) == 2


def test_empty_test_list_never_touches_the_cache(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches = _fake_pytest(monkeypatch, rcs=0)
    assert pr.run_pytest(repo, [], use_systemd=False, cache_dir=tmp_path / "c").passed
    assert not launches


def test_non_git_worktree_still_runs_uncached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree hash is unobtainable without git, and that must not break the run."""
    plain = tmp_path / "plain"
    plain.mkdir()
    launches = _fake_pytest(monkeypatch, rcs=0)
    assert pr.run_pytest(plain, ["t.py"], use_systemd=False, cache_dir=tmp_path / "c").passed
    assert len(launches) == 1


def test_default_cache_dir_is_under_agent_fleet() -> None:
    assert str(DEFAULT_CACHE_DIR) == "~/.agent-fleet/cache/gate-tests"


# ---------------------------------------------------------------------------
# Nested packages — the key must be the worktree root, not the package dir
# ---------------------------------------------------------------------------


@pytest.fixture
def nested_repo(tmp_path: Path) -> Path:
    """A repo owning tests in a nested ``api/`` package, importing root code.

    ``api/tests/test_a.py`` imports ``rootcode`` from the repo root, so the test
    observes code that lives outside its own package dir. A key computed from
    ``api/`` alone is blind to that code.
    """
    root = tmp_path / "nested"
    (root / "api" / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="root-pkg"\nversion="0.0.0"\n',
        encoding="utf-8",
    )
    (root / "rootcode.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "api" / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="api-pkg"\nversion="0.0.0"\n',
        encoding="utf-8",
    )
    (root / "api" / "tests" / "test_a.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))\n"
        "import rootcode\n"
        "def test_a():\n"
        "    assert rootcode.VALUE == 1\n",
        encoding="utf-8",
    )
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "gate@local", cwd=root)
    _git("config", "user.name", "gate", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def test_tree_hash_of_a_package_dir_sees_edits_outside_it(nested_repo: Path) -> None:
    """`worktree_tree_hash` honours its "WHOLE worktree" promise from any subdir."""
    before = worktree_tree_hash(nested_repo / "api")
    (nested_repo / "rootcode.py").write_text("VALUE = 99\n", encoding="utf-8")
    after = worktree_tree_hash(nested_repo / "api")

    assert before is not None
    assert after is not None
    assert before != after, (
        "hashing a subdirectory cannot see edits made outside it; the caller passes "
        "a package dir, so the hash must resolve up to the worktree root itself"
    )


def test_runner_misses_the_cache_when_root_code_changes(
    nested_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit to root code the nested test imports must invalidate the key."""
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)
    runner = GateTestRunner(
        root=nested_repo,
        memory="6G",
        timeout_s=60,
        use_systemd=False,
        cache_dir=cache,
        cache_ttl_s=3600,
    )
    assert [p.rel_dir for p in runner.packages_for(["api/tests/test_a.py"])] == ["api"]

    first = runner.run(["api/tests/test_a.py"])
    assert not first.tests_failed and not first.infra_error
    assert len(launches) == 1

    (nested_repo / "rootcode.py").write_text("VALUE = 2\n", encoding="utf-8")

    second = runner.run(["api/tests/test_a.py"])

    assert len(launches) == 2, (
        "a real change outside the package dir must miss the cache; a single launch "
        "means the gate replayed evidence for code it never re-ran"
    )
    assert not second.tests_failed and not second.infra_error
