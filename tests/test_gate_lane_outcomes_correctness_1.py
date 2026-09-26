"""Claim correctness-1: a blanket ``.agent-fleet/`` exclude drops real lane work.

``ensure_run_dir_excluded`` writes the whole-subtree pattern ``.agent-fleet/`` into
the repository's ``info/exclude``. The intent is narrow — keep the engine's run
logs out of commits — and the staging code honours that intent, unstaging only
``.agent-fleet/runs`` after an unconditional ``git add -A``.

The exclude line, however, is not narrow. In any repository that *tracks*
``.agent-fleet/`` (it is a real, committed config directory in this fleet's own
repos), the pattern hides the entire directory from git: files the lane creates
there never appear in ``git status --porcelain -uall``, so they are never staged
and never committed. A lane whose only work is under ``.agent-fleet/`` does not
merely lose a file — ``is_dirty`` reports a clean worktree and the guarantee
escalates ``no_commits_ahead`` on a lane that did real work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.worktree import ensure_lane_worktree, ensure_run_dir_excluded


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo that *tracks* ``.agent-fleet/`` — the ordinary case for this fleet."""
    root = tmp_path / "lake-of-rage"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "l@example.com")
    _git(root, "config", "user.name", "L")
    _git(root, "config", "commit.gpgsign", "false")

    (root / "README.md").write_text("base\n", encoding="utf-8")
    tracked = root / ".agent-fleet"
    tracked.mkdir()
    (tracked / "config.yaml").write_text("lanes: {}\n", encoding="utf-8")
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


# ------------------------------------------------------------------ the defect


def test_a_new_file_under_a_tracked_run_dir_is_still_committed(repo: Path, tmp_path: Path) -> None:
    """Work under a tracked ``.agent-fleet/`` must reach the auto-commit."""
    wt = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")

    # The lane added a new file to a directory this repo already tracks.
    (wt.path / ".agent-fleet" / "router.py").write_text("routes = []\n", encoding="utf-8")
    # ... alongside ordinary work, so the tree is dirty either way.
    (wt.path / "feature.py").write_text("x = 1\n", encoding="utf-8")

    assert g.is_dirty(wt.path) is True

    ok, sha, detail, _hooks = g.commit_worktree(wt.path, engine="cmd", lane="movers")

    assert ok, f"the lane's commit failed: {detail}"
    assert sha is not None
    names = _git(wt.path, "show", "--name-only", "--format=", "HEAD")
    assert ".agent-fleet/router.py" in names, (
        "a file the lane created under a tracked .agent-fleet/ was silently dropped "
        f"from the commit; committed: {names.split()}"
    )


def test_a_lanes_only_work_under_the_run_dir_is_not_reported_as_a_clean_lane(
    repo: Path, tmp_path: Path
) -> None:
    """The worse half: the work is invisible, so the lane escalates for nothing.

    ``is_dirty`` is the guarantee's precondition for committing. If the only file
    the lane produced is hidden by the exclude line, the worktree reads clean and
    the guarantee concludes there is nothing to publish.
    """
    wt = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    (wt.path / ".agent-fleet" / "router.py").write_text("routes = []\n", encoding="utf-8")

    assert g.is_dirty(wt.path) is True, (
        "the lane's only new file is invisible to git status, so the guarantee "
        "reads a clean worktree and escalates no_commits_ahead on a lane that "
        "did real work"
    )


def test_the_exclude_line_is_scoped_to_the_run_dir_not_the_whole_tree(
    repo: Path, tmp_path: Path
) -> None:
    """The exclude must not hide a directory the repository itself tracks.

    Only the engine's transcript subtree needs hiding; ``.agent-fleet/`` as a
    whole also swallows the lane's real work wherever that directory is tracked.
    """
    wt = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")

    assert ensure_run_dir_excluded(wt.path) in (True, False)  # idempotent second call
    exclude = Path(_git(wt.path, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = wt.path / exclude
    patterns = [
        line.strip() for line in exclude.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

    assert ".agent-fleet/" not in patterns, (
        "a blanket '.agent-fleet/' ignore hides every file a lane adds under a "
        f"tracked .agent-fleet/; info/exclude carries {patterns}"
    )
    assert ".agent-fleet/runs/" in patterns, (
        "the run dir itself must stay ignored, that is the whole point of the line; "
        f"info/exclude carries {patterns}"
    )
