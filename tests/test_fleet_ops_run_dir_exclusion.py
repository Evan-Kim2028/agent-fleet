"""Defect 1: the engine run dir must never end up in a commit or a PR.

Three production symptoms, one cause. The engine's run dir defaulted to
``<worktree>/.agent-fleet/runs/<lane>/`` and the PR guarantee stages with
``git add -A``:

* every PR the lane opened carried ``impl.jsonl`` / ``impl.out``;
* when the implementer changed nothing, the guarantee staged *only* the logs,
  the commit then ran the repo's hooks, and the lane reported ``commit_failed``
  — a worktree that was already clean reported as a hook failure;
* a worktree whose only untracked file was the run log was "dirty", so the
  guarantee kept trying to commit it.

The fix has two independent layers, because either alone is a single point of
failure: the default run dir moves *outside* the worktree, and the path is
registered in the repo's ``info/exclude`` so even an explicitly-passed run dir
inside the worktree is invisible to git. The guarantee additionally excludes
the path at stage time rather than trusting the exclude file to be correct.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import guarantee as g
from agent_fleet.fleet_ops.runner import LaneRunResult, default_run_dir, run_lane
from agent_fleet.fleet_ops.worktree import RUN_DIR_EXCLUDE_LINE, ensure_lane_worktree

if TYPE_CHECKING:
    from collections.abc import Callable

HEAD_SHA = "a" * 40

STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "opened the PR"}),
    ]
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
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


def test_the_default_run_dir_is_outside_the_worktree(tmp_path: Path) -> None:
    """The primary fix: run logs land under the fleet state dir, not in the repo."""
    workdir = tmp_path / "wt"
    workdir.mkdir()
    path = default_run_dir("documents-1d", "movers", run_id="run-1")
    assert workdir not in path.parents
    assert path.is_relative_to(tmp_path / "home")


def test_two_lane_runs_do_not_overwrite_each_others_logs() -> None:
    first = default_run_dir("documents-1d", "movers", run_id="run-1")
    second = default_run_dir("documents-1d", "movers", run_id="run-2")
    assert first != second


def test_ensure_lane_worktree_excludes_the_run_dir(repo: Path, tmp_path: Path) -> None:
    wt = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    exclude = Path(_git(wt.path, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = wt.path / exclude
    assert RUN_DIR_EXCLUDE_LINE in exclude.read_text(encoding="utf-8")


def test_excluding_the_run_dir_twice_adds_one_line(repo: Path, tmp_path: Path) -> None:
    """Idempotent: a reused lane must not accumulate duplicate exclude lines."""
    parent = tmp_path / "wt"
    first = ensure_lane_worktree(repo, lane="movers", parent=parent)
    second = ensure_lane_worktree(repo, lane="movers", parent=parent)
    assert first.path == second.path
    exclude = Path(_git(first.path, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = first.path / exclude
    assert exclude.read_text(encoding="utf-8").count(RUN_DIR_EXCLUDE_LINE) == 1


def test_the_excluded_run_dir_does_not_make_the_worktree_dirty(repo: Path, tmp_path: Path) -> None:
    """A run dir inside the worktree must not read as uncommitted work."""
    wt = ensure_lane_worktree(repo, lane="movers", parent=tmp_path / "wt")
    logs = wt.path / ".agent-fleet" / "runs" / "movers"
    logs.mkdir(parents=True)
    (logs / "impl.jsonl").write_text('{"type": "result"}\n', encoding="utf-8")
    assert g.is_dirty(wt.path) is False


def test_the_guarantee_never_stages_the_run_dir(repo: Path) -> None:
    """Defence in depth: the guarantee excludes the path at stage time.

    ``info/exclude`` covers the default case, but a caller can pass an explicit
    ``--run-dir`` inside the worktree and the exclude line only exists because
    ``ensure_lane_worktree`` wrote it. Staging must not depend on that having
    happened, so this calls the guarantee directly against a repo that never
    went through ``ensure_lane_worktree`` and has no exclude line at all.
    """
    lane_dir = repo / ".agent-fleet" / "runs" / "movers"
    lane_dir.mkdir(parents=True)
    (lane_dir / "impl.jsonl").write_text('{"type": "result"}\n', encoding="utf-8")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "checkout", "-b", "fb/movers")

    ok, sha, detail, _hooks = g.commit_worktree(repo, engine="cmd", lane="movers")

    assert ok, detail
    assert sha is not None
    names = _git(repo, "show", "--name-only", "--format=", "HEAD")
    assert "feature.py" in names
    assert ".agent-fleet" not in names


def test_a_lane_that_changed_nothing_never_reports_commit_failed(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The misreported ``commit_failed``, at the level it was actually observed.

    Before the fix, the run log made the worktree dirty, so the guarantee staged
    it, committed *only* it, and died on the repo's hooks — a lane that produced
    no work at all reported as a commit failure. The verdict is now a real
    no-work outcome, and the log is nowhere near the index.

    Which no-work reason it is depends on whether the implementer said anything
    (see ``test_fleet_ops_no_change_outcomes.py``); the point here is that it is
    never ``commit_failed``.
    """
    result = _run(repo, task_file, tmp_path)

    assert result.escalated
    assert result.reason != "commit_failed"
    if result.guarantee is not None:
        assert result.guarantee.committed is False
    # The log is still written — and still outside the repo.
    assert result.engine_result is not None
    assert result.engine_result.stream_path is not None
    assert result.worktree not in result.engine_result.stream_path.parents


def test_a_lane_run_leaves_the_repo_untracked_file_free(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """End to end: the PR the lane opens carries no run logs."""
    _git(repo, "checkout", "-b", "fb/movers")
    (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")

    result = _run(repo, task_file, tmp_path, run_dir=None)

    assert result.pr == 3544
    assert result.guarantee is not None and result.guarantee.committed is True
    assert ".agent-fleet" not in _git(repo, "show", "--name-only", "--format=", "HEAD")
    # ...and the logs really were written, just not into the repo.
    assert result.engine_result is not None
    assert result.engine_result.stream_path is not None
    assert "runs/documents-1d/movers" in str(result.engine_result.stream_path)


# ----------------------------------------------------------------- the lane


@pytest.fixture
def task_file(tmp_path: Path) -> Path:
    path = tmp_path / "task.md"
    path.write_text("# Fix the thing\n\nDo the work.\n", encoding="utf-8")
    return path


def _runner(
    slug: str = "Evan-Kim2028/lake-of-rage",
) -> Callable[..., subprocess.CompletedProcess[str]]:
    created: dict[str, int | None] = {"number": None}

    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, f"git@github.com:{slug}.git\n", "")
        if argv[:1] == ["gh"]:
            if argv[:3] == ["gh", "pr", "list"]:
                number = created["number"]
                payload = (
                    [{"number": number, "headRefName": "fb/movers", "headRefOid": HEAD_SHA}]
                    if number
                    else []
                )
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            if argv[:3] == ["gh", "pr", "create"]:
                created["number"] = 3544
                return subprocess.CompletedProcess(
                    argv, 0, "https://github.com/o/r/pull/3544\n", ""
                )
        if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
            return subprocess.CompletedProcess(argv, 0, STREAM, "")
        return subprocess.run(argv, **kwargs)

    return runner


def _run(repo: Path, task: Path, tmp_path: Path, **kwargs: Any) -> LaneRunResult:  # noqa: ANN401
    from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec

    # No classification flag is needed here: STREAM's final text is an ordinary
    # "opened the PR", which is not an unfinished intention, so a lane that
    # changed nothing falls straight through to the guarantee — which is exactly
    # what this file is about.
    opts: dict[str, Any] = {
        "operator": "documents-1d",
        "lane": "movers",
        "repo_path": repo,
        "task_file": task,
        "engine": "cmd",
        "config": FleetOpsConfig(operators={"documents-1d": OperatorSpec(name="documents-1d")}),
        "status_file": tmp_path / "lane.status",
        "worktree_parent": tmp_path / "wt",
        "known_gate_subcommands": {"run"},
        "runner": _runner(),
    }
    return run_lane(**{**opts, **kwargs})
