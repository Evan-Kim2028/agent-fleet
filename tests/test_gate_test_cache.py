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
from pathlib import Path

import pytest

from agent_fleet.gate import pytest_runner as pr
from agent_fleet.gate.pipeline import GateTestRunner
from agent_fleet.gate.pytest_runner import (
    DEFAULT_CACHE_DIR,
    PytestResult,
    _relative_package,
    cache_key,
    cache_load,
    cache_store,
    ignored_files_digest,
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


# ---------------------------------------------------------------------------
# Cross-worktree reuse — the key must not carry an absolute path
# ---------------------------------------------------------------------------


def test_identical_trees_in_sibling_worktrees_share_one_cache_entry(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's own worktrees hold the same tree; they must share the entry.

    ``converge()`` runs the same test list in ``<gate>/wt``, ``recheck``,
    ``reN`` and ``final``. Keying on the absolute package directory gave each
    one its own key, so pytest was re-launched for a tree that had not changed
    — the exact reuse the cache exists to provide.
    """
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)
    # A real repo ignores its venv, so it is absent from the tree hash. Without
    # this the .venv below is merely untracked, and `git add -A` folds it into
    # the tree — which correctly splits the key and tests nothing.
    (repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "ignore venv", cwd=repo)

    worktrees = []
    for name in ("wt", "recheck", "re1", "final"):
        path = tmp_path / ".agent-fleet" / "gate" / "123" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-q", "--detach", str(path), "HEAD", cwd=repo)
        # Each gate worktree has its own virtualenv, and an editable install
        # writes that worktree's absolute path into it. Hashing those bytes
        # would split the key again — through the back door this test guards.
        venv = path / ".venv"
        venv.mkdir()
        (venv / "activate").write_text(f'VIRTUAL_ENV="{path / ".venv"}"\n', encoding="utf-8")
        (venv / "link").symlink_to("/usr/bin/python3")
        worktrees.append(path)

    for path in worktrees:
        pr.run_pytest(path, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert len(launches) == 1, (
        f"{len(worktrees)} worktrees holding one identical tree must cost one pytest "
        f"launch, got {len(launches)}; the key is carrying the absolute package dir "
        "or worktree-specific ignored-file bytes"
    )


def test_ignored_venv_bytes_do_not_split_the_key_across_worktrees(repo: Path) -> None:
    """The digest must stay free of per-worktree virtualenv contents.

    This is the failure the first version of the ignored-file digest had: it
    hashed every ignored byte, and ``.venv/bin/activate`` plus the editable
    install finders embed the worktree's own absolute path, so two worktrees of
    one commit disagreed and every fix round re-ran pytest. Dependency and build
    directories are excluded, so their drift cannot split the key.
    """
    digests = set()
    for name in ("wt", "recheck"):
        path = repo.parent / "gate" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-q", "--detach", str(path), "HEAD", cwd=repo)
        venv = path / ".venv"
        venv.mkdir()
        (venv / "activate").write_text(f'VIRTUAL_ENV="{path / ".venv"}"\n', encoding="utf-8")
        (venv / "pkgs").mkdir()
        (venv / "pkgs" / "RECORD").write_text(f"editable install at {path}\n", encoding="utf-8")
        digests.add(ignored_files_digest(path))

    assert digests == {""}, (
        f"worktree-specific .venv bytes reached the digest ({digests}); two worktrees "
        "of one commit must agree, or the cache never reuses across them"
    )


def test_an_ignored_fixture_is_still_covered_with_a_venv_present(repo: Path) -> None:
    """Excluding build dirs must not blind the digest to real test input."""
    (repo / ".gitignore").write_text("secret.env\n.venv/\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "ign", cwd=repo)
    (repo / ".venv").mkdir()
    (repo / ".venv" / "activate").write_text('VIRTUAL_ENV="/x/.venv"\n', encoding="utf-8")
    (repo / "secret.env").write_text("TOKEN = first\n", encoding="utf-8")
    before = ignored_files_digest(repo)

    (repo / "secret.env").write_text("TOKEN = second\n", encoding="utf-8")
    assert ignored_files_digest(repo) != before

    (repo / "secret.env").write_text("TOKEN = first\n", encoding="utf-8")
    assert ignored_files_digest(repo) == before, "the digest must not drift on its own"


def test_cache_key_is_identical_for_the_same_tree_at_different_paths(repo: Path) -> None:
    """Pin the root cause directly: same tree, different directories, one key."""
    trees = set()
    keys = set()
    for name in ("wt", "recheck", "final"):
        path = repo.parent / "gate" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-q", "--detach", str(path), "HEAD", cwd=repo)
        tree = worktree_tree_hash(path)
        assert tree is not None
        trees.add(tree)
        keys.add(
            cache_key(
                tree,
                ["tests/test_a.py"],
                package=_relative_package(path, path),
                ignored=ignored_files_digest(path),
            )
        )

    assert len(trees) == 1, "fixture must produce worktrees of one identical tree"
    assert len(keys) == 1, (
        f"one tree reached through {len(keys)} directories produced {len(keys)} keys; "
        "an absolute path in the key defeats every cross-worktree reuse"
    )


def test_a_nested_package_still_keys_differently_from_the_root(nested_repo: Path) -> None:
    """Dropping the absolute path must not merge two genuinely different packages.

    ``.`` and ``api`` name different files; collapsing them into one key would
    replay a result for one package as if it were the other's.
    """
    tree = worktree_tree_hash(nested_repo)
    assert tree is not None
    root_pkg = _relative_package(nested_repo, nested_repo)
    api_pkg = _relative_package(nested_repo, nested_repo / "api")
    root_key = cache_key(tree, ["tests/test_a.py"], package=root_pkg)
    api_key = cache_key(tree, ["api/tests/test_a.py"], package=api_pkg)

    assert root_pkg == "."
    assert api_pkg == "api"
    assert root_key != api_key


# ---------------------------------------------------------------------------
# Gitignored files — the tree hash cannot see them
# ---------------------------------------------------------------------------


@pytest.fixture
def ignored_repo(repo: Path) -> Path:
    """*repo* with a gitignored ``secret.env`` that a test reads."""
    (repo / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "ignore secret.env", cwd=repo)
    (repo / "secret.env").write_text("TOKEN = first\n", encoding="utf-8")
    return repo


def test_editing_a_gitignored_file_misses_the_cache(
    ignored_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: a stale hit after an edit git could not see.

    ``git add -A`` skips ignored files, so the tree hash is identical before and
    after the edit. Serving the cached result would report a verdict produced
    from different bytes than the ones the test would now read.
    """
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)

    pr.run_pytest(ignored_repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    (ignored_repo / "secret.env").write_text("TOKEN = second\n", encoding="utf-8")
    pr.run_pytest(ignored_repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)

    assert len(launches) == 2, (
        "an edit to a gitignored file must invalidate the cached result; one launch "
        "means a stale verdict was replayed"
    )


def test_adding_or_removing_a_gitignored_file_misses_the_cache(
    ignored_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Presence is part of the key, not just content."""
    cache = tmp_path / "cache"
    launches = _fake_pytest(monkeypatch, rcs=0)

    pr.run_pytest(ignored_repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    (ignored_repo / "secret.env").unlink()
    pr.run_pytest(ignored_repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    assert len(launches) == 2, "removing a gitignored file must miss"

    (ignored_repo / "secret.env").write_text("TOKEN = third\n", encoding="utf-8")
    pr.run_pytest(ignored_repo, ["tests/test_a.py"], use_systemd=False, cache_dir=cache)
    assert len(launches) == 3, "re-adding a gitignored file with new content must miss"


def test_ignored_digest_is_stable_and_content_sensitive(ignored_repo: Path) -> None:
    """The digest must not churn on its own, or every run would miss."""
    assert ignored_files_digest(ignored_repo) == ignored_files_digest(ignored_repo)

    before = ignored_files_digest(ignored_repo)
    (ignored_repo / "secret.env").write_text("TOKEN = second\n", encoding="utf-8")
    after = ignored_files_digest(ignored_repo)
    assert before != after

    (ignored_repo / "secret.env").write_text("TOKEN = first\n", encoding="utf-8")
    assert ignored_files_digest(ignored_repo) == before


def test_ignored_digest_ignores_tracked_files(repo: Path) -> None:
    """Only ignored files belong in the digest; tracked ones are already in the tree."""
    before = ignored_files_digest(repo)
    (repo / "code.py").write_text("VALUE = 7\n", encoding="utf-8")
    assert ignored_files_digest(repo) == before


def test_a_repo_with_no_ignored_files_yields_an_empty_digest(repo: Path) -> None:
    assert ignored_files_digest(repo) == ""


def test_ignored_digest_survives_an_unreadable_ignored_file(
    ignored_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that cannot be read must not take the gate down with it.

    The walk falls back to the file's path, so the digest differs from a
    readable one. That is the fail-safe direction: the content was not proven
    unchanged, so the key must not claim it was.
    """
    readable = ignored_files_digest(ignored_repo)
    real_open = Path.open

    def exploding_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "secret.env":
            raise PermissionError("gone")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", exploding_open)
    unreadable = ignored_files_digest(ignored_repo)
    monkeypatch.undo()

    assert len(unreadable) == 64, "an unreadable file must still yield a digest, not raise"
    assert unreadable != readable, (
        "content that could not be read was not proven unchanged, so the key must differ"
    )
    assert ignored_files_digest(ignored_repo) == readable, "the failure must not persist"
