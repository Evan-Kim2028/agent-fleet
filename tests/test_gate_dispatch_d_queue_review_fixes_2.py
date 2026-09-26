"""Regression tests for the second PR #121 review round.

One test per claim, each written to fail against the pre-fix code:

* ``test_the_pretty_printed_lane_result_is_parsed`` — spec-1:
  ``read_lane_result`` only accepted a single-line object starting with ``{``,
  but ``lane run --json`` prints ``json.dumps(..., indent=2)``. Not one lane
  result ever parsed, so every lane was reaped ``no_pr`` and no gate ran.
* ``test_a_zero_lane_bound_is_refused`` — prodsafety-1:
  ``max_lanes=0`` makes ``plan_tick`` plan no launch and makes an empty
  in-flight set count as a finished queue, so the loop broke on its first tick
  having launched nothing, tallied zero errors and exited 0 over a queue that
  never ran.
* ``test_a_lane_that_actually_started_is_recorded_before_the_launch_returns`` —
  prodsafety-2: the child's identity was written only into the state the caller
  saves after the action list finishes, so a failure past the spawn left a live
  child recorded as QUEUED with no identity -- and the next run read that as a
  crashed lane and launched a second child for the same work.
* ``test_a_reclaimed_lane_does_not_hand_a_still_running_child_its_worktree`` —
  prodsafety-2, follow-on: recovery cleared the pid but kept the worktree, so a
  relaunch handed a second agent the checkout the first child still owned.
* ``test_a_zero_throttle_bound_is_refused`` — prodsafety-3:
  ``--max-throttle-ticks 0`` made the first idle tick trip the bound, collapsing
  the bounded wait into a single tick, and it was the one bound the CLI never
  validated.
* ``test_the_operators_pinned_judge_reaches_the_queue_dispatcher`` —
  correctness-3: the queue path never supplied ``judge_engine``, so the
  per-operator ``judge_engine`` pin was unreachable for every dispatched PR and
  the gate silently fell back to its own default judge.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import dispatch as dispatch_mod
from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.cli import _throttle_ticks
from agent_fleet.fleet_ops.dispatch import (
    DISPATCH_DONE,
    DISPATCH_QUEUED,
    DISPATCH_RUNNING,
    DispatchItem,
    DispatchState,
    gate_argv,
    load_state,
    read_lane_result,
    run_dispatch,
    status_file_for,
)
from agent_fleet.fleet_ops.runner import LaneRunResult

if TYPE_CHECKING:
    from collections.abc import Sequence

IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


class _Proc:
    """A spawned child that exits after *polls* calls; ``None`` never exits."""

    def __init__(self, pid: int, exit_code: int | None = 0, *, polls: int = 0) -> None:
        self.pid = pid
        self.returncode = exit_code
        self._polls = polls

    def poll(self) -> int | None:
        if self._polls is None:
            return None
        if self._polls > 0:
            self._polls -= 1
            return None
        return self.returncode


def _item(lane: str) -> DispatchItem:
    return DispatchItem.from_dict(
        {"lane": lane, "repo": "acme", "task": f"do {lane}", "ref": f"R-{lane}"}
    )


def _queue_and_repo(tmp_path: Path, *lanes: str) -> tuple[Path, dict[str, str]]:
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(
            json.dumps({"lane": lane, "repo": "acme", "task": f"do {lane}", "ref": f"R-{lane}"})
            for lane in lanes
        ),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return queue, {"acme": str(repo)}


def _lane_run_json(**overrides: Any) -> str:  # noqa: ANN401
    """Exactly what ``fleet lane run --json`` prints: ``indent=2``."""
    fields: dict[str, Any] = {
        "lane": "a",
        "operator": "op",
        "engine": "claude",
        "branch": "fb/a",
        "worktree": "/wt/a",
        "pr": 42,
        "state": "pr_guaranteed",
    }
    fields.update(overrides)
    result = LaneRunResult(**fields)
    return json.dumps(result.to_dict(), indent=2, default=str)


def test_the_pretty_printed_lane_result_is_parsed() -> None:
    """The one format ``lane run --json`` actually emits must be readable."""
    stdout = _lane_run_json()

    pr, worktree, _detail = read_lane_result(stdout)

    assert pr == 42, (
        f"read_lane_result returned {pr!r} for the real `lane run --json` output; "
        "only a single-line object parsed, so every lane was reaped no_pr and no "
        "gate ever launched"
    )
    assert worktree == "/wt/a"


def test_a_single_line_lane_result_still_parses() -> None:
    """The format that used to work must not regress."""
    pr, worktree, _detail = read_lane_result(json.dumps({"state": "pr_guaranteed", "pr": 7}))

    assert (pr, worktree) == (7, None)


def test_log_chatter_around_a_result_still_parses() -> None:
    """A lane's real log is not only JSON."""
    stdout = (
        "starting lane\n"
        "gh: creating pull request\n" + _lane_run_json() + "\ntraceback (most recent call last):\n"
    )

    pr, _worktree, _detail = read_lane_result(stdout)

    assert pr == 42


def test_a_guaranteed_pr_reaches_the_gate_end_to_end(tmp_path: Path) -> None:
    """End to end: a lane that promised a PR must produce a gate run."""
    queue, repos = _queue_and_repo(tmp_path, "a")
    out = tmp_path / "out"
    gated: list[str] = []

    def spawn(argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        argv = list(argv)
        if argv[:3] == ["fleet", "lane", "run"]:
            with kwargs["stdout"] as log:
                log.write(_lane_run_json(lane="a", pr=42))
            return _Proc(4242, 0, polls=1)
        lane = argv[argv.index("--lane") + 1]
        gated.append(lane)
        status = status_file_for(out, lane)
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text("12:00:00 PREMERGE-APPROVED abc1234def\n", encoding="utf-8")
        return _Proc(4343, 0)

    summary = run_dispatch(
        operator="op",
        queue_path=queue,
        repos=repos,
        max_lanes=2,
        max_gates=1,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )

    assert gated == ["a"], (
        f"no gate was launched ({gated!r}); the lane's PR was read as absent so the "
        "queue was never reviewed or merged"
    )
    assert summary.approved == 1
    assert load_state("op").lanes["a"].state == DISPATCH_DONE


@pytest.mark.parametrize(
    ("bound", "value"),
    [("max_lanes", 0), ("max_gates", 0), ("throttle_max_ticks", 0)],
)
def test_a_zero_bound_is_refused(tmp_path: Path, bound: str, value: int) -> None:
    """A bound that cannot admit work must be refused, not honoured.

    ``max_lanes=0`` was the reported case: no launch is ever planned, an empty
    in-flight set counts as a finished queue, and the run exits 0 having done
    nothing. The other two are the same class of nonsense number and are refused
    at the same place so none of them can reach the loop.
    """
    queue, repos = _queue_and_repo(tmp_path, "a", "b")

    with pytest.raises(ValueError, match=bound):
        run_dispatch(
            operator="op",
            queue_path=queue,
            repos=repos,
            spawn=lambda *_a, **_k: pytest.fail("nothing may be spawned"),
            psi_reader=lambda: IDLE,
            sleep=lambda _s: None,
            run_dir=tmp_path / "out",
            **{bound: value},
        )


def test_a_zero_lane_bound_never_reports_a_dropped_queue_as_success(tmp_path: Path) -> None:
    """The reported symptom: launched=0, errors=0, exit 0, lanes still queued."""
    queue, repos = _queue_and_repo(tmp_path, "a", "b")
    launched: list[list[str]] = []

    def spawn(argv: Sequence[str], **_kwargs: Any) -> Any:  # noqa: ANN401
        launched.append(list(argv))
        return _Proc(1, 0, polls=1)

    try:
        summary = run_dispatch(
            operator="op",
            queue_path=queue,
            repos=repos,
            max_lanes=0,
            spawn=spawn,
            psi_reader=lambda: IDLE,
            sleep=lambda _s: None,
            run_dir=tmp_path / "out",
        )
    except ValueError:
        return

    states = {name: lane.state for name, lane in summary.state.lanes.items()}
    pytest.fail(
        f"max_lanes=0 returned instead of refusing: launched={summary.launched} "
        f"errors={summary.errors} exit={summary.exit_code()} states={states}"
    )


def test_a_lane_that_actually_started_is_recorded_before_the_launch_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is durable the moment it exists, not when the tick ends.

    The identity used to be written only into the state the caller persists
    after the whole action list, so anything that failed between the spawn and
    that save left a live child recorded as QUEUED with no pid. The next run
    reads that as a crashed lane, requeues it and launches a second child for
    the same work -- and the first is never reaped, because there is no handle
    and no recorded pid to probe.

    The failure is injected on the tick that spawns, which is the tick the
    window exists on; saving then failing is a full disk, and an unpersistable
    event append right after the spawn fails the same way.
    """
    queue, repos = _queue_and_repo(tmp_path, "a")
    real_event = dispatch_mod.append_event
    state_after_spawn: list[DispatchState] = []

    def spawn(_argv: Sequence[str], **kwargs: Any) -> Any:  # noqa: ANN401
        with kwargs["stdout"] as log:
            log.write("starting\n")
        return _Proc(7777, None, polls=None)

    def boom(_state: DispatchState, lane: str, event: str, **_fields: Any) -> None:  # noqa: ANN401
        if event == "dispatch.launched":
            state_after_spawn.append(dispatch_mod.load_state("op"))
            raise OSError("no space left on device")
        real_event(_state, lane, event, **_fields)

    monkeypatch.setattr(dispatch_mod, "append_event", boom)

    run_dispatch(
        operator="op",
        queue_path=queue,
        repos=repos,
        max_lanes=1,
        max_gates=1,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=tmp_path / "out",
    )

    persisted = state_after_spawn[-1].lanes["a"] if state_after_spawn else None
    assert persisted is not None and persisted.state == DISPATCH_RUNNING, (
        f"immediately after spawning, the durable state says "
        f"{getattr(persisted, 'state', None)!r} with pid "
        f"{getattr(persisted, 'lane_pid', None)!r}; a live child recorded as "
        f"{DISPATCH_QUEUED!r} with no pid is read by the next run as a crashed "
        "lane, which relaunches a second agent on the same work"
    )


def test_a_reclaimed_lane_does_not_hand_a_still_running_child_its_worktree() -> None:
    """Recovery clears the pid, so the worktree must go with it."""
    record = dispatch_mod.merge_queue(DispatchState(operator="op"), (_item("a"),)).lanes["a"]
    running = dataclasses.replace(
        record,
        state=DISPATCH_RUNNING,
        lane_pid=None,
        lane_pgid=4242,
        worktree="/wt/a",
    )
    state = dataclasses.replace(DispatchState(operator="op"), lanes={"a": running}, procs={})

    recovered = dispatch_mod._recover_lane(
        state, dispatch_mod.RecoverLane("a", reason="no_lane_process")
    )

    lane = recovered.lanes["a"]
    assert lane.state == DISPATCH_QUEUED
    assert lane.worktree is None, (
        f"the recovered lane still holds {lane.worktree!r}; the relaunch reuses the "
        "recorded worktree, so a second agent would be pointed at a checkout the "
        "first child still owns"
    )


def test_reclaiming_a_never_spawned_lane_keeps_its_recorded_worktree() -> None:
    """A lane with no child at all has no worktree to protect, and the test
    above must not have bought that by clearing every worktree."""
    record = dispatch_mod.merge_queue(DispatchState(operator="op"), (_item("a"),)).lanes["a"]
    state = dataclasses.replace(
        DispatchState(operator="op"),
        lanes={"a": dataclasses.replace(record, state=DISPATCH_QUEUED, worktree="/wt/a")},
        procs={},
    )
    action = dispatch_mod.RecoverLane("a", reason="no_lane_process")
    state = dispatch_mod._recover_lane(state, action)
    state = dataclasses.replace(
        state, lanes={"a": dataclasses.replace(state.lanes["a"], state=DISPATCH_RUNNING)}
    )

    recovered = dispatch_mod._recover_lane(
        state, dispatch_mod.RecoverLane("a", reason="no_lane_process")
    )

    assert recovered.lanes["a"].worktree == "/wt/a"


def test_a_zero_throttle_bound_is_refused() -> None:
    """The one bound the CLI never validated."""
    assert _throttle_ticks(argparse.Namespace(max_throttle_ticks=None)) == (
        dispatch_mod.DEFAULT_MAX_THROTTLE_TICKS
    )
    for bad in (0, -1):
        with pytest.raises(ValueError, match="--max-throttle-ticks"):
            _throttle_ticks(argparse.Namespace(max_throttle_ticks=bad))


def test_a_pinned_judge_reaches_the_dispatched_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``operators.NAME.judge_engine`` must apply to queue dispatch too.

    ``lane run`` honours it; the queue path never passed the value on, so every
    dispatched PR was judged by the gate's own default instead of the judge the
    operator pinned -- silently, with nothing in the argv to show for it.
    """
    repo = tmp_path / "acme"
    repo.mkdir()
    (repo / ".agent-fleet.yaml").write_text(
        "fleet_ops:\n  operators:\n    op:\n      judge_engine: grok\n",
        encoding="utf-8",
    )
    captured: dict[str, list[str]] = {}

    def fake_run_dispatch(**kwargs: Any) -> Any:  # noqa: ANN401
        captured["judge_engine"] = kwargs.get("judge_engine")
        return dispatch_mod.DispatchSummary(operator=kwargs["operator"], lanes=1)

    monkeypatch.setattr("agent_fleet.fleet_ops.cli.run_dispatch", fake_run_dispatch)
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")

    from agent_fleet.fleet_ops.cli import cmd_dispatch_queue

    args = argparse.Namespace(
        queue=str(queue),
        operator="op",
        max_lanes=None,
        max_gates=None,
        gate_cmd=None,
        repo=["acme=" + str(repo)],
        psi_avg10_max=None,
        tick_seconds=None,
        max_throttle_ticks=None,
        judge_engine=None,
        json=True,
    )
    cmd_dispatch_queue(args)

    assert captured.get("judge_engine") == "grok", (
        f"run_dispatch received judge_engine={captured.get('judge_engine')!r}; the "
        "operator pinned grok, so the gate judged on its own default instead"
    )


def test_an_explicit_judge_flag_overrides_the_operator_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flag on the command line is the operator's last word."""
    repo = tmp_path / "acme"
    repo.mkdir()
    (repo / ".agent-fleet.yaml").write_text(
        "fleet_ops:\n  operators:\n    op:\n      judge_engine: grok\n", encoding="utf-8"
    )
    captured: dict[str, Any] = {}

    def fake_run_dispatch(**kwargs: Any) -> Any:  # noqa: ANN401
        captured["judge_engine"] = kwargs.get("judge_engine")
        return dispatch_mod.DispatchSummary(operator=kwargs["operator"], lanes=1)

    monkeypatch.setattr("agent_fleet.fleet_ops.cli.run_dispatch", fake_run_dispatch)
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")

    from agent_fleet.fleet_ops.cli import cmd_dispatch_queue

    cmd_dispatch_queue(
        argparse.Namespace(
            queue=str(queue),
            operator="op",
            max_lanes=None,
            max_gates=None,
            gate_cmd=None,
            repo=["acme=" + str(repo)],
            psi_avg10_max=None,
            tick_seconds=None,
            max_throttle_ticks=None,
            judge_engine="cmd",
            json=True,
        )
    )

    assert captured.get("judge_engine") == "cmd"


def test_an_operator_with_no_pin_leaves_the_gate_to_choose() -> None:
    """No pin must mean "gate default", not an invented value."""
    from agent_fleet.fleet_ops.config import FleetOpsConfig

    config = FleetOpsConfig()
    spec = config.operator("nobody")

    assert spec is None
    assert (spec.judge_engine if spec else None) is None


def test_the_judge_flag_is_registered_and_reaches_gate_argv() -> None:
    """The flag has to exist to be usable, and the value has to arrive."""
    from agent_fleet.fleet_ops import cli as fleet_ops_cli

    root = argparse.ArgumentParser(prog="agent-fleet")
    sub = root.add_subparsers(dest="command", required=True)
    fleet_ops_cli.register_dispatch_command(sub)
    args = root.parse_args(["dispatch", "q.jsonl", "--operator", "op", "--judge-engine", "cmd"])

    assert args.judge_engine == "cmd"
    argv = gate_argv(
        None,
        lane="a",
        pr=1,
        repo="acme",
        operator="op",
        slug="Evan-Kim2028/acme",
        status_file="/s",
        repo_path="/r",
        task_file="/t",
        judge_engine=args.judge_engine,
    )
    assert "--judge-engine" in argv
    assert argv[argv.index("--judge-engine") + 1] == "cmd"
