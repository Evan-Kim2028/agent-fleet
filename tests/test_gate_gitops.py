"""Tests for the gate's git/GitHub plumbing (real repos in tmp_path)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime

import pytest

from agent_fleet.gate.gitops import (
    GateError,
    PullRequestRef,
    changed_test_files,
    fetch_base,
    prepare_worktree,
    remove_worktree,
    resolve_pull_request,
    worktree_head_sha,
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
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit on main and a feature branch adding a test."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _git(root, "checkout", "-q", "-b", "feature")
    (root / "tests").mkdir()
    (root / "tests" / "test_new.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (root / "agent.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feature")
    return root


# ---------------------------------------------------------------------------
# PullRequestRef
# ---------------------------------------------------------------------------


def test_pr_ref_is_open_and_short_sha() -> None:
    ref = PullRequestRef(number=7, head_ref="fb/lane", head_sha="abcdef1234", state="OPEN")
    assert ref.is_open
    assert ref.short_sha == "abcdef123"


def test_pr_ref_closed_is_not_open() -> None:
    assert not PullRequestRef(1, "b", "sha", "MERGED").is_open
    assert not PullRequestRef(1, "b", "sha", "closed").is_open


# ---------------------------------------------------------------------------
# worktrees
# ---------------------------------------------------------------------------


def test_prepare_worktree_creates_a_detached_checkout(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    assert (wt / "agent.py").is_file()
    assert worktree_head_sha(wt) == head
    # Detached: no branch is checked out, so a crash cannot half-update one.
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_prepare_worktree_replaces_a_stale_one(repo: Path, tmp_path: Path) -> None:
    old = _git(repo, "rev-parse", "HEAD~1")
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", old)
    assert (wt / "agent.py").read_text() == "VALUE = 1\n"
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    assert (wt / "agent.py").read_text() == "VALUE = 2\n"


def test_prepare_worktree_at_a_missing_sha_raises(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(GateError, match="worktree add"):
        prepare_worktree(repo, tmp_path / "wt", "0" * 40)


def test_remove_worktree_is_forgiving(repo: Path, tmp_path: Path) -> None:
    """A missing worktree must not raise — cleanup runs in a finally block."""
    remove_worktree(repo, tmp_path / "never-existed")
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    remove_worktree(repo, wt)
    remove_worktree(repo, wt)  # idempotent
    assert not wt.exists()


def test_remove_worktree_drops_uncommitted_changes(repo: Path, tmp_path: Path) -> None:
    """A fixer that left junk behind must not block the next run's worktree."""
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    (wt / "junk.txt").write_text("x", encoding="utf-8")
    remove_worktree(repo, wt)
    assert not wt.exists()
    # A fresh worktree at the same sha still works.
    assert (prepare_worktree(repo, tmp_path / "wt2", head) / "agent.py").is_file()


# ---------------------------------------------------------------------------
# changed_test_files
# ---------------------------------------------------------------------------


def test_changed_test_files_finds_the_prs_new_test(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    # Compare against main, which this fixture has locally.
    assert changed_test_files(wt, "main") == ["tests/test_new.py"]


def test_changed_test_files_excludes_non_tests(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    assert "agent.py" not in changed_test_files(wt, "main")


def test_changed_test_files_is_empty_for_no_test_changes(repo: Path, tmp_path: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    assert changed_test_files(wt, "HEAD") == []


def test_changed_test_files_skips_deleted_tests(repo: Path, tmp_path: Path) -> None:
    """A test deleted by the PR must not be run at head."""
    _git(repo, "rm", "-q", "tests/test_new.py")
    _git(repo, "commit", "-q", "-m", "delete the test")
    head = _git(repo, "rev-parse", "HEAD")
    wt = prepare_worktree(repo, tmp_path / "wt", head)
    assert changed_test_files(wt, "main") == []


# ---------------------------------------------------------------------------
# resolve_pull_request
# ---------------------------------------------------------------------------


def test_resolve_pull_request_reads_the_head(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "headRefName": "feature",
        "headRefOid": "abc123",
        "state": "OPEN",
        "baseRefName": "main",
    }
    _install_fake_gh(monkeypatch, payload)
    ref = resolve_pull_request(repo, 5)
    assert ref.number == 5
    assert ref.head_ref == "feature"
    assert ref.head_sha == "abc123"
    assert ref.is_open
    assert ref.base_ref == "main"


def test_resolve_pull_request_raises_on_a_nonzero_exit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_gh(monkeypatch, None, returncode=1, stderr="no such PR")
    with pytest.raises(GateError, match="no such PR"):
        resolve_pull_request(repo, 999)


def test_resolve_pull_request_raises_on_bad_json(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_gh(monkeypatch, None, stdout="not json at all")
    with pytest.raises(GateError, match="unparseable"):
        resolve_pull_request(repo, 1)


def test_fetch_base_tolerates_a_remote_that_does_not_exist(repo: Path) -> None:
    """No origin remote: fetch must be best-effort, not fatal."""
    fetch_base(repo, "main")  # must not raise


def test_worktree_head_sha_on_a_broken_path(tmp_path: Path) -> None:
    assert worktree_head_sha(tmp_path) == ""


def _install_fake_gh(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str] | None,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Patch subprocess.run so the gate's gh calls are deterministic."""
    out = stdout or (json.dumps(payload) if payload is not None else "")

    class _Completed:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = out
            self.stderr = stderr

    monkeypatch.setattr(
        "agent_fleet.gate.gitops.subprocess.run",
        lambda *_a, **_kw: _Completed(),
    )
