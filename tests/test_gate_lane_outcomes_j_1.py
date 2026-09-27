"""A tracked ``.agent-fleet/`` directory must not break the auto-commit.

The guarantee stages with ``git add -A -- . ':(exclude).agent-fleet'``. That
pathspec removes the path from the *add*; ``git add -A`` then falls back to its
untracked-file list, finds the ``.agent-fleet`` entries (ignored via
``info/exclude``), and refuses:

    The following paths are ignored by one of your .gitignore files: .agent-fleet

with exit code 1 — and the index keeps *only* the paths that were added before
the walk hit the ignored entry.

The documented rationale for the pathspec is that an explicit ``--run-dir``
inside the worktree, or a repo whose exclude file could not be written, must not
be able to put a transcript into a commit. But when ``.agent-fleet/`` is a
*tracked* directory holding the lane's real work, the same pathspec does not
"exclude the log" — it fails the whole ``add`` and the lane's real change to
that directory is silently left unstaged while the lane reports
``commit_failed``.

This is distinct from the orphan-log case: there the directory was untracked
(only an ignored run log inside it); here git tracks ``.agent-fleet/keep.txt``,
which is why the modification shows up in ``git status`` yet never reaches the
index.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.worktree import RUN_DIR_EXCLUDE_LINE


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo that *tracks* a real ``.agent-fleet/keep.txt``.

    The dir is committed before the exclude line is written — i.e. the state a
    repo reaches if a lane ever put genuine work under ``.agent-fleet/`` and it
    was committed upstream.
    """
    root = tmp_path / "lake-of-rage"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "l@example.com")
    _git(root, "config", "user.name", "L")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")

    tracked = root / ".agent-fleet"
    tracked.mkdir()
    (tracked / "keep.txt").write_text("real work\n", encoding="utf-8")

    _git(root, "add", "-A", "-f", "--", ".agent-fleet")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")

    # Now the run dir is also ignored, exactly as ensure_lane_worktree leaves it.
    exclude = Path(_git(root, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = root / exclude
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{RUN_DIR_EXCLUDE_LINE}\n")

    _git(root, "checkout", "-b", "fb/movers")
    return root


def test_a_tracked_run_dir_directory_does_not_break_the_auto_commit(repo: Path) -> None:
    """The lane's real change under a tracked ``.agent-fleet/`` must be committed.

    Both changes are genuine work: the modification to the tracked
    ``.agent-fleet/keep.txt`` and the edit to ``a.py``. The guarantee must
    commit them. A transcript placed in the run dir in the same breath must
    still be left out.
    """
    (repo / ".agent-fleet" / "keep.txt").write_text("real work, edited\n", encoding="utf-8")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    logs = repo / ".agent-fleet" / "runs" / "movers"
    logs.mkdir(parents=True)
    (logs / "impl.jsonl").write_text('{"type": "result"}\n', encoding="utf-8")

    # Precondition: git itself sees both edits as unstaged work.
    assert g.is_dirty(repo) is True
    status = _git(repo, "status", "--porcelain")
    assert ".agent-fleet/keep.txt" in status

    ok, sha, detail, _hooks = g.commit_worktree(repo, engine="cmd", lane="movers")

    # The commit must happen...
    assert ok, f"auto-commit failed: {detail}"
    assert sha is not None
    # ...and it must carry the lane's real change to the tracked run dir.
    assert not g.is_dirty(repo), f"work left unstaged: {_git(repo, 'status', '--porcelain')}"
    names = _git(repo, "show", "--name-only", "--format=", "HEAD")
    assert "a.py" in names
    assert ".agent-fleet/keep.txt" in names
    # ...while the transcript is still not in it.
    assert "impl.jsonl" not in names
