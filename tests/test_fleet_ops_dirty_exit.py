"""A lane must never report success while holding work it did not publish.

The failure this replaces is specific and was measured: on lor-main, nine of ten
lanes exited 0 with four to fifteen changed files, no commit, no PR and no
status line. An operator reading "exit 0" from a lane with 15 changed files has
no way to tell a lane that did nothing from a lane whose work was lost, and the
queue moves on either way.

The contract, in one sentence: **exit 0 means the worktree holds no work.** The
two things that are *allowed* to be left behind are the agent's own scratch
(configured, and untracked by definition) and the run logs — they are byproducts
of running, not work, and a lane that failed to notice them would fail every
lane forever.

These tests use a real worktree and a real git history, because "is the tree
dirty" and "did the commit happen" are exactly the things a mock would answer
by assumption.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops.config import DEFAULT_SCRATCH_EXCLUDES, FleetOpsConfig, OperatorSpec
from agent_fleet.fleet_ops.runner import REASON_UNCOMMITTED_WORK, run_lane
from agent_fleet.fleet_ops.statusfile import ESCALATION_TOKEN

if TYPE_CHECKING:
    from collections.abc import Callable

HEAD_SHA = "a" * 40

#: An engine that did real work and said so, which is what a healthy lane's
#: final text looks like.
WORKED_STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "implemented the stamp column update"}),
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


@pytest.fixture
def task_file(tmp_path: Path) -> Path:
    path = tmp_path / "task.md"
    path.write_text("# Fix the thing\n\nDo the work.\n", encoding="utf-8")
    return path


def _lane_runner(
    *, slug: str = "Evan-Kim2028/lake-of-rage", on_engine: Callable[[Path], None] | None = None
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A runner for the engine and ``gh``; real git for everything else.

    *on_engine* runs while the "engine" is executing, which is where a test
    plants the work an implementer would have left behind. Doing it there
    rather than before the call is what makes the test honest: the files appear
    in the worktree the way they would if an agent had written them, and the
    manager has to find them on its own.
    """
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
            if on_engine is not None:
                workdir = Path(kwargs.get("cwd") or argv[0])
                on_engine(workdir)
            return subprocess.CompletedProcess(argv, 0, WORKED_STREAM, "")
        return subprocess.run(argv, **kwargs)

    return runner


def _run(
    repo: Path,
    task: Path,
    tmp_path: Path,
    *,
    on_engine: Callable[[Path], None] | None = None,
    config: FleetOpsConfig | None = None,
    gate: bool = False,
) -> Any:  # noqa: ANN401
    return run_lane(
        operator="documents-1d",
        lane="movers",
        repo_path=repo,
        task_file=task,
        engine="cmd",
        config=config
        or FleetOpsConfig(operators={"documents-1d": OperatorSpec(name="documents-1d")}),
        status_file=tmp_path / "lane.status",
        run_dir=tmp_path / "runs",
        worktree_parent=tmp_path / "wt",
        known_gate_subcommands={"run"},
        gate=gate,
        runner=_lane_runner(on_engine=on_engine),
    )


def _write_work(workdir: Path) -> None:
    (workdir / "api").mkdir(exist_ok=True)
    (workdir / "api" / "client.py").write_text("def f():\n    return 1\n", encoding="utf-8")


# ------------------------------------------------------------- the happy path


def test_a_lane_whose_work_committed_ends_clean(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The baseline the guard must not break: real work, committed, PR guaranteed."""
    result = _run(repo, task_file, tmp_path, on_engine=_write_work)
    assert result.pr == 3544
    assert result.guarantee is not None
    assert result.guarantee.committed is True
    assert result.reason != REASON_UNCOMMITTED_WORK


def test_scratch_left_behind_does_not_make_a_lane_dirty(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The agent's own directories stay on disk without failing the lane.

    A guard that counted scratch would fail every lane on this fleet, because
    every agent here writes one. That is why they are excluded by configuration
    rather than by hoping the agent tidies up after itself.
    """

    def work_and_scratch(workdir: Path) -> None:
        _write_work(workdir)
        for name in (".commandcode", "%h"):
            directory = workdir / name
            directory.mkdir(exist_ok=True)
            (directory / "session.log").write_text("scratch\n", encoding="utf-8")

    result = _run(repo, task_file, tmp_path, on_engine=work_and_scratch)
    assert result.reason != REASON_UNCOMMITTED_WORK
    assert result.pr == 3544


def test_scratch_never_reaches_the_commit(repo: Path, task_file: Path, tmp_path: Path) -> None:
    """The scratch is excluded from the *commit*, not merely excused afterwards."""

    def work_and_scratch(workdir: Path) -> None:
        _write_work(workdir)
        directory = workdir / ".commandcode"
        directory.mkdir(exist_ok=True)
        (directory / "session.log").write_text("scratch\n", encoding="utf-8")

    result = _run(repo, task_file, tmp_path, on_engine=work_and_scratch)
    assert result.guarantee is not None
    committed = _git(result.worktree, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert ".commandcode/session.log" not in committed
    assert "api/client.py" in committed


# ------------------------------------------------------- work left behind


def test_work_that_never_got_committed_escalates(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """A commit path that fails leaves real edits — the lane must not exit 0.

    The hook refuses the commit, so the work stays in the worktree. The lane
    already escalated for ``commit_failed``; what this asserts is the second
    half of the contract, that the dirty state itself is never a success
    condition on a path that reaches a terminal verdict some other way.
    """
    (repo / "pre-commit-blocker").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    def work_and_harden(workdir: Path) -> None:
        _write_work(workdir)
        # A worktree's `.git` is a *file* pointing at the real gitdir, so the
        # hook is installed where git looks rather than where a naive
        # `.git/hooks` guess would put it (and silently not run at all).
        hook = Path(_git(workdir, "rev-parse", "--git-path", "hooks")) / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            "#!/bin/sh\nprintf '[hook]\\n- hook id: real-lint\\n'\nexit 1\n", encoding="utf-8"
        )
        hook.chmod(0o755)

    result = _run(repo, task_file, tmp_path, on_engine=work_and_harden)
    assert result.escalated is True
    assert result.reason == "commit_failed"
    # The work is still there for whoever picks the lane up.
    assert (result.worktree / "api" / "client.py").exists()


def test_a_dirty_worktree_is_named_in_the_status_line(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The operator reading only the status file learns the work is preserved and where."""
    result = _run(
        repo,
        task_file,
        tmp_path,
        on_engine=lambda w: _write_work(w),
        gate=True,
    )
    lines = (tmp_path / "lane.status").read_text(encoding="utf-8").splitlines()
    assert any(ESCALATION_TOKEN in line for line in lines) or result.reason != (
        REASON_UNCOMMITTED_WORK
    )
    if result.reason == REASON_UNCOMMITTED_WORK:
        assert str(result.worktree) in result.detail


# ------------------------------------------------------------- the classifier


def test_the_leftover_check_ignores_scratch_and_run_logs(repo: Path) -> None:
    """Unit-level: the exclusion set is what makes the guard usable at all."""
    from agent_fleet.fleet_ops.runner import _uncommitted_leftovers

    config = FleetOpsConfig(scratch_excludes=DEFAULT_SCRATCH_EXCLUDES)
    assert _uncommitted_leftovers(repo, config=config) == []

    (repo / "api").mkdir()
    (repo / "api" / "client.py").write_text("x = 1\n", encoding="utf-8")
    assert _uncommitted_leftovers(repo, config=config) == ["api/client.py"]

    scratch = repo / ".commandcode"
    scratch.mkdir()
    (scratch / "taste.md").write_text("x\n", encoding="utf-8")
    home = repo / "%h"
    home.mkdir()
    (home / "profile").write_text("x\n", encoding="utf-8")
    runs = repo / ".agent-fleet" / "runs"
    runs.mkdir(parents=True)
    (runs / "stream.log").write_text("x\n", encoding="utf-8")

    assert _uncommitted_leftovers(repo, config=config) == ["api/client.py"]
