"""Claim correctness-2: a finished lane keeps a signallable process identity.

The lane manager records its own ``(pid, pgid, starttime)`` before spawning the
engine, precisely so a concurrent ``lanes stop`` has something valid to signal.
The matching teardown is the single ``update_record(..., pid=None, pgid=None,
starttime=None)`` that runs once the engine is done.

The two early-return escalations added for ``lazy_exit`` and ``no_changes_stopped``
return *before* that teardown, so a lane that has already exited finishes with
the live process group of the finished manager still recorded in the registry.
``stop_lane_by_name`` does not filter on state, so a later "stop the movers"
finds a record that still looks like a running lane and signals a process group
this lane no longer owns.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec
from agent_fleet.fleet_ops.registry import load_record
from agent_fleet.fleet_ops.runner import run_lane

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HEAD_SHA = "a" * 40

#: A run that ends mid-intention. The first pass triggers the nudge, the second
#: is still lazy, so the lane escalates as ``lazy_exit`` via the early return.
LAZY_STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "Now I'll update the manifest:"}),
    ]
)

#: A run that states its own reason and stops on purpose. No intention phrasing,
#: so it takes the ``no_changes_stopped`` early return.
STOPPED_STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps(
            {
                "type": "result",
                "finalText": (
                    "I did not change anything. The task asks me to edit a file that is "
                    "under an explicit owner fence, so editing it would reverse another "
                    "session's decision. Routing this to the owner instead."
                ),
            }
        ),
    ]
)


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


@pytest.fixture
def task_file(tmp_path: Path) -> Path:
    path = tmp_path / "task.md"
    path.write_text("# Fix the thing\n\nDo the work.\n", encoding="utf-8")
    return path


def _lane_runner(stream: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    def runner(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        argv = list(args)
        if argv[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(
                argv, 0, "git@github.com:Evan-Kim2028/lake-of-rage.git\n", ""
            )
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
            return subprocess.CompletedProcess(argv, 0, stream, "")
        return subprocess.run(argv, **kwargs)

    return runner


def _run(repo: Path, task: Path, tmp_path: Path, stream: str) -> None:
    run_lane(
        operator="documents-1d",
        lane="movers",
        repo_path=repo,
        task_file=task,
        engine="cmd",
        config=FleetOpsConfig(operators={"documents-1d": OperatorSpec(name="documents-1d")}),
        status_file=tmp_path / "lane.status",
        run_dir=tmp_path / "runs",
        worktree_parent=tmp_path / "wt",
        known_gate_subcommands={"run"},
        runner=_lane_runner(stream),
    )


# ------------------------------------------------------------------ the defect


def test_a_lazy_exit_lane_clears_its_process_identity(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """``lazy_exit`` escalates through the early return, before the teardown."""
    _run(repo, task_file, tmp_path, LAZY_STREAM)

    record = load_record("documents-1d", "movers")
    assert record is not None
    assert record.reason == "lazy_exit", f"precondition: got {record.reason!r}"
    assert record.pid is None, (
        f"a finished lane still records pid {record.pid}; the registry hands a later "
        "'lanes stop' a process identity for a process that has already exited"
    )
    assert record.pgid is None
    assert record.starttime is None


def test_a_no_changes_stopped_lane_clears_its_process_identity(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """``no_changes_stopped`` escalates through the other early return."""
    _run(repo, task_file, tmp_path, STOPPED_STREAM)

    record = load_record("documents-1d", "movers")
    assert record is not None
    assert record.reason == "no_changes_stopped", f"precondition: got {record.reason!r}"
    assert record.pid is None, (
        f"a finished lane still records pid {record.pid}/pgid {record.pgid}; the "
        "registry hands a later 'lanes stop' a signallable identity for a lane that "
        "has already exited"
    )
    assert record.pgid is None
    assert record.starttime is None


def test_the_recorded_identity_is_not_left_pointing_at_a_dead_process(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """Once ``run_lane`` returns, nothing it recorded may still be signalable.

    ``update_record`` writes the manager's own pid/pgid *before* the engine
    spawn, and one teardown clears them afterwards. Skipping that teardown
    leaves the record naming a process that has already returned — and the
    recycled-pid guard exists precisely to catch a number that has been reused,
    so a stale identity is the one input it cannot distinguish from a live lane.
    """
    _run(repo, task_file, tmp_path, STOPPED_STREAM)

    record = load_record("documents-1d", "movers")
    assert record is not None
    # The lane run is over. Any identity still on the record now belongs to a
    # process that no longer exists.
    assert (record.pid, record.pgid, record.starttime) == (None, None, None), (
        "run_lane returned but the lane record still holds "
        f"pid={record.pid} pgid={record.pgid} starttime={record.starttime}"
    )
