"""Claim prodsafety-1: a finished lane leaves a stale pgid that ``stop_lane`` can signal.

This is the safety consequence of the two early-return escalations. The lane
manager records its own ``(pid, pgid, starttime)`` before spawning the engine so
a concurrent ``lanes stop`` has a valid target, and one ``update_record(..., pid=
None, pgid=None, starttime=None)`` clears it once the engine is done. The
``lazy_exit`` and ``no_changes_stopped`` early returns skip that teardown, so a
lane that has already finished keeps its process identity in the registry.

``stop_lane_by_name`` does not filter on lane state, and ``stop_lane`` only
refuses when the recorded pid is dead or its start-time fingerprint has changed.
A record whose ``starttime`` is ``None`` skips the recycled-pid check entirely,
and one whose fingerprint still matches is indistinguishable from a live lane. On
a host running many agents, ``os.killpg`` on a stale pgid can land on an
unrelated process tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import stop as stop_mod
from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec
from agent_fleet.fleet_ops.registry import LaneRecord, load_record
from agent_fleet.fleet_ops.runner import run_lane
from agent_fleet.fleet_ops.stop import stop_lane, verify_process_identity

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HEAD_SHA = "a" * 40

LAZY_STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "result", "finalText": "Now I'll update the manifest:"}),
    ]
)

STOPPED_STREAM = "\n".join(
    [
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps({"type": "tool_completed", "subtype": "completed"}),
        json.dumps(
            {
                "type": "result",
                "finalText": (
                    "I did not change anything. The file is under an explicit owner "
                    "fence, so editing it would reverse another session's decision. "
                    "Routing this to the owner instead."
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


#: Drives one lane in a *real* subprocess with its own process group, so the
#: identity the lane records is a genuinely foreign pgid — the production shape.
#: Running the lane in-process would record the test's own group, which
#: ``stop_lane`` refuses for a different reason and which proves nothing.
_LANE_DRIVER = """
import json, subprocess, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from agent_fleet.fleet_ops.config import FleetOpsConfig, OperatorSpec
from agent_fleet.fleet_ops.runner import run_lane

STREAM = {stream!r}


def runner(args, **kwargs):
    argv = list(args)
    if argv[:3] == ["git", "remote", "get-url"]:
        return subprocess.CompletedProcess(argv, 0, "git@github.com:o/r.git\\n", "")
    if argv[:3] == ["gh", "pr", "list"]:
        return subprocess.CompletedProcess(argv, 0, "[]", "")
    if any(str(a).endswith("cmd") for a in argv[:3]) or "--max-turns" in argv:
        return subprocess.CompletedProcess(argv, 0, STREAM, "")
    return subprocess.run(argv, **kwargs)


run_lane(
    operator="documents-1d",
    lane="movers",
    repo_path=Path({repo!r}),
    task_file=Path({task!r}),
    engine="cmd",
    config=FleetOpsConfig(operators={{"documents-1d": OperatorSpec(name="documents-1d")}}),
    status_file=Path({status!r}),
    run_dir=Path({rundir!r}),
    worktree_parent=Path({wt!r}),
    known_gate_subcommands={{"run"}},
    runner=runner,
)
"""


def _run_lane_in_own_process_group(
    repo: Path, task: Path, tmp_path: Path, stream: str
) -> subprocess.CompletedProcess[str]:
    """Run the lane under ``setsid`` so its recorded pgid is its own."""
    script = _LANE_DRIVER.format(
        repo=str(repo),
        task=str(task),
        status=str(tmp_path / "lane.status"),
        rundir=str(tmp_path / "runs"),
        wt=str(tmp_path / "wt"),
        stream=stream,
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        start_new_session=True,
        # The driver must write to the same registry the test reads back.
        env={**os.environ},
    )


# ------------------------------------------------------------------ the defect


def test_a_finished_lane_leaves_no_process_identity_in_the_registry(
    repo: Path, task_file: Path, tmp_path: Path
) -> None:
    """The safety invariant, stated directly.

    ``run_lane`` has returned. Nothing about this lane is running any more, so
    the registry must not name a process for it. This is the one teardown the two
    early-return escalations skip.
    """
    _run(repo, task_file, tmp_path, LAZY_STREAM)

    record = load_record("documents-1d", "movers")
    assert record is not None
    assert record.state == "escalated"
    assert (record.pid, record.pgid, record.starttime) == (None, None, None), (
        "a lane that escalated via the early return kept a live-looking process "
        f"identity (pid={record.pid} pgid={record.pgid} starttime={record.starttime}) "
        "after the lane had already exited"
    )


@pytest.mark.parametrize(
    ("stream", "expected_reason"),
    [(LAZY_STREAM, "lazy_exit"), (STOPPED_STREAM, "no_changes_stopped")],
    ids=["lazy_exit", "no_changes_stopped"],
)
def test_stopping_a_finished_lane_refuses_rather_than_signalling(
    repo: Path,
    task_file: Path,
    tmp_path: Path,
    stream: str,
    expected_reason: str,
) -> None:
    """A stopped lane must be *signalled*, not left signallable.

    ``stop_lane`` refuses with ``refused_no_process`` only when the record holds
    no identity at all. If the identity survives the lane, that refusal never
    fires and the function proceeds toward ``os.killpg`` on a process group the
    lane no longer owns.

    The lane runs in its own process group (and has already exited by the time
    ``stop_lane`` sees the record), which is the production shape: the recorded
    pgid is a real, foreign group, not the caller's own.

    With the teardown in place this record is empty, so ``stop_lane`` refuses up
    front with ``refused_no_process`` and never considers signalling. Because the
    teardown is skipped, the record still names a process group, and ``stop_lane``
    has to fall through to its liveness logic instead — which is only
    ``already_gone`` here by the accident that this particular pid has since
    exited. On a host where that number has been recycled, the same record passes
    ``verify_process_identity`` and reaches ``os.killpg``.
    """
    done = _run_lane_in_own_process_group(repo, task_file, tmp_path, stream)
    assert done.returncode == 0, f"the lane driver failed: {done.stderr[-800:]}"

    record = load_record("documents-1d", "movers")
    assert record is not None
    assert record.reason == expected_reason, f"precondition: got {record.reason!r}"

    outcome = stop_lane(record, own_pgid_value=os.getpgid(0), sleep=lambda _s: None)

    # The refusal that matters is "there is nothing here to signal", decided from
    # the record alone. Reaching a liveness verdict (``already_gone``,
    # ``refused_pid_reused``, or an actual signal) means the record still named a
    # process group for a lane that exited.
    assert outcome.reason == stop_mod.REFUSED_NO_PROCESS, (
        f"a finished lane ({expected_reason}) left a signallable identity "
        f"(pid={record.pid} pgid={record.pgid}); stop_lane fell through to its "
        f"liveness logic and returned {outcome.reason!r} instead of refusing "
        "up front on an empty record"
    )
    assert outcome.signalled is False


def test_a_stale_record_can_reach_killpg_on_an_unrelated_process_group(
    repo: Path, task_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concrete safety risk: a stale identity the guard vouches for.

    ``verify_process_identity`` refuses a dead pid and a changed start-time
    fingerprint, but it skips the fingerprint check entirely when ``starttime``
    is ``None``. The identity the early returns leave behind therefore passes as
    a live lane whenever its pid is live, and ``stop_lane`` proceeds to
    ``os.killpg`` on that pgid — which, on a host with many agents, may belong to
    an unrelated process tree.

    The signal is *intercepted* here: the point is that ``killpg`` is called at
    all, not that a real group is terminated.
    """
    # A live process in its own group, standing in for an unrelated agent tree.
    bystander = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        done = _run_lane_in_own_process_group(repo, task_file, tmp_path, STOPPED_STREAM)
        assert done.returncode == 0, f"the lane driver failed: {done.stderr[-800:]}"
        record = load_record("documents-1d", "movers")
        assert record is not None
        # The defect: the finished lane still named a process group.
        assert record.pgid is not None, "precondition: the early return cleared the identity"

        # The hazard: that number can be recycled. A record holding a live pid
        # with no start-time fingerprint verifies clean, so stop_lane treats a
        # lane that exited as a lane that is running.
        stale = LaneRecord(
            lane=record.lane,
            operator=record.operator,
            pid=bystander.pid,
            pgid=bystander.pid,
            starttime=None,
        )
        ok, reason = verify_process_identity(stale)
        assert not (ok and reason == "ok"), (
            "a record with no start-time fingerprint must not verify clean; that is "
            "the one input the recycled-pid guard cannot distinguish from a live lane"
        )

        signalled: list[int] = []
        monkeypatch.setattr(
            stop_mod.os, "killpg", lambda pgid, _sig: signalled.append(pgid), raising=True
        )
        monkeypatch.setattr(stop_mod, "process_alive", lambda _pid: True, raising=True)

        stop_lane(stale, own_pgid_value=bystander.pid + 1, sleep=lambda _s: None)

        assert signalled == [], (
            f"stop_lane signalled process group {signalled} for a lane that had "
            "already exited; on a shared host that group can belong to another agent"
        )
    finally:
        bystander.terminate()
        bystander.wait(timeout=10)
