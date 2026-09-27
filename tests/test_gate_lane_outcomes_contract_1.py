"""Contract: a lane with real work commits even when a stale in-worktree run dir exists.

``ensure_lane_worktree`` writes ``.agent-fleet/`` into ``info/exclude`` and its
docstring says that is precisely the case this protects: "a worktree created by an
older build still has the old in-worktree run dir on disk". In that state
``commit_worktree`` must still commit the lane's real changes, and must still keep
the run dir out of the commit.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.worktree import ensure_lane_worktree

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "lake-of-rage"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "l@example.com")
    _git(root, "config", "user.name", "L")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")

    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-u", "origin", "main")
    return root


def test_real_work_commits_despite_a_stale_in_worktree_run_dir(repo: Path, tmp_path: Path) -> None:
    wt = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")

    # A run dir left inside the worktree by an older build, in exactly the shape
    # ensure_lane_worktree's docstring says it protects against -- and now ignored
    # via the info/exclude line that call just wrote.
    stale = wt.path / ".agent-fleet" / "runs" / "movers"
    stale.mkdir(parents=True)
    (stale / "impl.jsonl").write_text('{"type": "result"}\n', encoding="utf-8")

    # The lane did real work that must land in the auto-commit.
    (wt.path / "feature.py").write_text("x = 1\n", encoding="utf-8")

    ok, sha, detail, _hooks = g.commit_worktree(wt.path, engine="cmd", lane="movers")

    assert ok, f"lane with real work failed to commit: {detail}"
    assert sha is not None
    names = _git(wt.path, "show", "--name-only", "--format=", "HEAD")
    assert "feature.py" in names
    assert ".agent-fleet" not in names
