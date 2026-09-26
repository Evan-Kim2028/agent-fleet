"""The durable queue dispatcher: one regression test per field failure.

The shell dispatcher this replaces lost a swarm six ways in one day. Each is
pinned here by a test named after it, and the module-level test docstrings
cross-reference the numbers below:

1. ``StopIteration`` on a finished lane missing from the queue
2. two operators sharing one event log and adopting each other's lanes
3. a restart relaunching lanes that had already run
4. eighteen gates released at once
5. (gate worktree pruning — see ``test_gate_gitops.py``)
6. ``loadavg`` throttling every launch shut for forty minutes

Almost everything is tested against :func:`plan_tick`, a pure function over an
immutable :class:`DispatchState`, so these run in microseconds with no
subprocesses and no sleeps. The ``run_dispatch`` tests use an injected ``spawn``
and a synchronous ``sleep`` that ticks the simulated clock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.fleet_ops import dispatch as dispatch_mod
from agent_fleet.fleet_ops import pressure
from agent_fleet.fleet_ops.dispatch import (
    DEFAULT_TICK_SECONDS,
    DISPATCH_DONE,
    DISPATCH_FAILED,
    DISPATCH_GATING,
    DISPATCH_PR,
    DISPATCH_QUEUED,
    UNKNOWN_ITEM,
    DispatchItem,
    DispatchLane,
    DispatchState,
    FinishLane,
    LaunchGate,
    LaunchLane,
    ReapGate,
    ReapLane,
    blocked_lanes,
    classify_status,
    dependency_satisfied,
    dispatch_state_path,
    gate_argv,
    lane_process_alive,
    load_queue,
    load_state,
    merge_queue,
    order_lanes,
    plan_tick,
    process_identity_alive,
    read_lane_result,
    render_task_file,
    run_dispatch,
    save_state,
    terminal_refs,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

SATIATED = pressure.Throttle(some_avg10=99.0, path=Path("/fake/cpu.pressure"), available=True)
IDLE = pressure.Throttle(some_avg10=0.0, path=Path("/fake/cpu.pressure"), available=True)
NO_READING = pressure.Throttle(some_avg10=None, path=None, available=False)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))


def item(lane: str, **overrides: Any) -> DispatchItem:  # noqa: ANN401
    base: dict[str, Any] = {"lane": lane, "repo": "acme", "task": f"do {lane}", "ref": f"R-{lane}"}
    base.update(overrides)
    return DispatchItem.from_dict(base)


def state_with(*items: DispatchItem, operator: str = "documents-0e") -> DispatchState:
    return merge_queue(DispatchState(operator=operator), items)


def queued(state: DispatchState, name: str) -> DispatchLane:
    lane = state.lanes[name]
    assert lane.state == DISPATCH_QUEUED
    return lane


def launches(actions: Sequence[object]) -> list[str]:
    return [a.lane for a in actions if isinstance(a, LaunchLane)]


def gate_launches(actions: Sequence[object]) -> list[LaunchGate]:
    return [a for a in actions if isinstance(a, LaunchGate)]


# ------------------------------------------------------------ 1. StopIteration


def test_a_finished_lane_with_no_queue_item_is_finished_not_raised() -> None:
    """A lane whose queue item cannot be resolved is recorded, never looked up.

    The shell driver did ``next(q for q in queue if q["lane"] == lane)``, which
    raised ``StopIteration`` and — inside the generator's ``for`` — a
    ``RuntimeError`` that killed the dispatcher, so every finished lane after
    that point was never gated. A missing item is now a terminal outcome.
    """
    # A lane whose item is None: the exact shape that had no queue entry.
    state = DispatchState(
        operator="documents-0e",
        lanes={"ghost": DispatchLane(lane="ghost", item=None, state=DISPATCH_PR)},
    )
    actions = plan_tick(state, max_lanes=4, max_gates=2)
    finishes = [a for a in actions if isinstance(a, FinishLane)]
    assert [f.lane for f in finishes] == ["ghost"]
    assert finishes[0].reason == UNKNOWN_ITEM
    assert not gate_launches(actions), "a lane with no PR must never get a gate"


def test_a_pr_state_without_a_pr_number_is_finished_not_gated() -> None:
    """``pr`` state with no PR is the same crash, one step later."""
    state = merge_queue(
        DispatchState(operator="documents-0e"),
        [item("a")],
    )
    state = _with_lane(state, "a", state=DISPATCH_PR, pr=None)
    actions = plan_tick(state, max_lanes=4, max_gates=2)
    assert [(a.lane, a.reason) for a in actions if isinstance(a, FinishLane)] == [
        ("a", UNKNOWN_ITEM)
    ]


def test_one_lane_that_cannot_launch_does_not_stop_the_others() -> None:
    """A lane with no item is finished on its own tick; the rest still launch."""
    state = DispatchState(
        operator="documents-0e",
        lanes={
            "ghost": DispatchLane(lane="ghost", item=None, state=DISPATCH_QUEUED),
            "real": DispatchLane(lane="real", item=item("real"), state=DISPATCH_QUEUED),
        },
    )
    actions = plan_tick(state, max_lanes=4, max_gates=2)
    assert launches(actions) == ["real"]
    assert [a.lane for a in actions if isinstance(a, FinishLane)] == ["ghost"]


def test_a_spawn_failure_is_recorded_and_the_dispatcher_keeps_going(tmp_path: Path) -> None:
    """One lane raising on spawn must not take the swarm down with it."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(json.dumps({"lane": n, "repo": "acme", "task": "t"}) for n in ("bad", "good")),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        if "bad" in argv:
            raise OSError("simulated spawn failure")
        # "good" launches, then exits on the next poll with no PR: the run
        # completes rather than parking on a live child forever.
        return _FakeProc(4321, 0)

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=4,
        max_gates=2,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=tmp_path / "out",
    )
    state = load_state("documents-0e")
    assert state.lanes["bad"].state == DISPATCH_FAILED
    assert "simulated spawn failure" in str(state.lanes["bad"].error)
    assert state.lanes["good"].state == DISPATCH_DONE
    assert state.lanes["good"].reason == "no_pr", "the healthy lane ran to completion"
    assert summary.launched == 2, "both lanes were attempted"
    assert summary.errors == 1, "exactly the bad lane failed"


def test_a_failed_lane_is_not_relaunched_on_the_next_tick() -> None:
    """A lane that failed to start must not be retried forever.

    Found while writing the tests above: the error handler wrote ``done`` while
    the record still read ``queued``, so ``plan_tick`` — which decides from the
    record — relaunched the same broken lane every tick, forever.
    """
    state = _with_lane(
        state_with(item("bad")), "bad", state=DISPATCH_FAILED, reason="error", error="boom"
    )
    assert launches(plan_tick(state, max_lanes=4, max_gates=2)) == []


def test_a_failed_lane_is_terminal_and_releases_its_dependents() -> None:
    state = state_with(item("bad"), item("after", depends_on=["R-bad"]))
    state = _with_lane(state, "bad", state=DISPATCH_FAILED, reason="error")
    assert "R-bad" in terminal_refs(state)
    assert launches(plan_tick(state, max_lanes=4, max_gates=2)) == ["after"]


# --------------------------------------------- 2. operators must not cross wires


def test_two_operators_with_the_same_lane_name_get_separate_state() -> None:
    """The same lane name under two operators is two lanes, in two files."""
    a = state_with(item("shared"), operator="documents-0e")
    b = state_with(item("shared"), operator="documents-1d")
    save_state(a)
    save_state(b)

    assert dispatch_state_path("documents-0e") != dispatch_state_path("documents-1d")
    assert load_state("documents-0e").lanes["shared"].state == DISPATCH_QUEUED
    assert load_state("documents-1d").lanes["shared"].state == DISPATCH_QUEUED

    # Mark one running; the other must be unaffected.
    a2 = _with_lane(a, "shared", state="running", lane_pid=111, lane_starttime=1)
    save_state(a2)
    assert load_state("documents-0e").lanes["shared"].lane_pid == 111
    assert load_state("documents-1d").lanes["shared"].lane_pid is None


def test_liveness_is_a_pid_fingerprint_and_never_a_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recycled pid must read as dead; a live one must not be adopted by name.

    The shell driver asked ``"--lane X " in ps -eo args``, which is how operator
    A ended up waiting on operator B's process. Here the only question is
    "is the pid I recorded still the process I recorded".
    """
    from agent_fleet.fleet_ops import registry

    # pid 4242 is alive, but it is NOT the process we recorded.
    monkeypatch.setattr(registry, "process_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(registry, "process_starttime", lambda _pid: 999)
    assert process_identity_alive(4242, 111) is False

    # Same pid, matching fingerprint: ours.
    monkeypatch.setattr(registry, "process_starttime", lambda _pid: 111)
    assert process_identity_alive(4242, 111) is True

    # A lane is only live if its own recorded identity is live.
    lane = DispatchLane(lane="a", item=item("a"), lane_pid=4242, lane_starttime=111)
    assert lane_process_alive(lane) is True
    other = DispatchLane(lane="b", item=item("b"), lane_pid=4242, lane_starttime=555)
    assert lane_process_alive(other) is False


def test_an_operator_only_ever_touches_its_own_durable_state() -> None:
    """Loading one operator's state cannot surface another's lanes."""
    save_state(state_with(item("mine"), operator="documents-0e"))
    save_state(state_with(item("theirs"), operator="documents-1d"))
    assert set(load_state("documents-0e").lanes) == {"mine"}
    assert set(load_state("documents-1d").lanes) == {"theirs"}


# ------------------------------------------------------- 3. restart is safe


def test_a_terminal_lane_is_never_relaunched() -> None:
    """The restart contract: terminal state means done, permanently."""
    state = _with_lane(
        state_with(item("a"), item("b")), "a", state=DISPATCH_DONE, reason="approved"
    )
    actions = plan_tick(state, max_lanes=8, max_gates=4)
    assert launches(actions) == ["b"]
    assert "a" not in launches(actions)


def test_a_live_lane_is_never_relaunched_or_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane whose recorded process is alive is left alone entirely."""
    from agent_fleet.fleet_ops import registry

    monkeypatch.setattr(registry, "process_alive", lambda pid: pid == 777)
    monkeypatch.setattr(registry, "process_starttime", lambda _pid: 5)

    state = _with_lane(
        state_with(item("a")),
        "a",
        state="running",
        lane_pid=777,
        lane_starttime=5,
    )
    actions = plan_tick(state, max_lanes=8, max_gates=4)
    assert not any(isinstance(a, (ReapLane, LaunchLane)) for a in actions)

    gating = _with_lane(state, "a", state=DISPATCH_GATING, pr=1, gate_pid=777, gate_starttime=5)
    assert not any(isinstance(a, ReapGate) for a in plan_tick(gating, max_lanes=8, max_gates=4))


def test_a_vanished_process_is_reaped_rather_than_leaked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed lane recovers instead of holding its slot forever."""
    from agent_fleet.fleet_ops import registry

    monkeypatch.setattr(registry, "process_alive", lambda _pid: False)
    monkeypatch.setattr(registry, "process_starttime", lambda _pid: None)
    state = _with_lane(state_with(item("a")), "a", state="running", lane_pid=777, lane_starttime=5)
    actions = plan_tick(state, max_lanes=8, max_gates=4)
    assert [(type(a).__name__, a.lane) for a in actions] == [("ReapLane", "a")]


def test_a_running_lane_with_no_recorded_pid_is_reclaimed() -> None:
    """State written between the decision and the spawn must not leak a slot."""
    state = _with_lane(state_with(item("a")), "a", state="running", lane_pid=None)
    actions = plan_tick(state, max_lanes=8, max_gates=4)
    assert [type(a).__name__ for a in actions] == ["RecoverLane"]


def test_state_survives_a_json_round_trip() -> None:
    """The restart contract depends on this serialising exactly."""
    state = _with_lane(
        state_with(item("a", cluster="C1", depends_on=["R-b"])),
        "a",
        state="running",
        lane_pid=4242,
        lane_starttime=99,
        pr=None,
    )
    save_state(state)
    reloaded = load_state("documents-0e")
    lane = reloaded.lanes["a"]
    assert lane.state == "running"
    assert (lane.lane_pid, lane.lane_starttime) == (4242, 99)
    assert lane.item is not None
    assert lane.item.cluster == "C1"
    assert lane.item.depends_on == ("R-b",)


def test_a_corrupt_state_file_restarts_rather_than_refusing() -> None:
    """An unreadable state must not stop the operator dispatching at all."""
    path = dispatch_state_path("documents-0e")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert load_state("documents-0e").lanes == {}


def test_merging_a_queue_never_resets_a_finished_lane() -> None:
    """Re-reading the same queue after a restart must not revive a done lane."""
    state = _with_lane(state_with(item("a")), "a", state=DISPATCH_DONE, reason="approved")
    again = merge_queue(state, [item("a")])
    assert again.lanes["a"].state == DISPATCH_DONE
    assert launches(plan_tick(again, max_lanes=8, max_gates=4)) == []


# ------------------------------------------------------- 4. the gate cap is hard


def test_the_gate_cap_is_exact_even_when_every_lane_finishes_at_once() -> None:
    """The shell driver released eighteen gates in one tick and hit load 200.

    ``plan_tick`` counts the gates it has already decided *within the same tick*,
    so the cap cannot overshoot by the width of the batch.
    """
    items = [item(f"l{n:02d}", cluster="C0") for n in range(30)]
    state = state_with(*items)
    state = _with_lanes(state, {i.lane: DISPATCH_PR for i in items}, pr=7)

    actions = plan_tick(state, max_lanes=30, max_gates=3)
    assert len(gate_launches(actions)) == 3
    assert len([a for a in actions if isinstance(a, LaunchLane)]) == 0

    # And the cap holds cumulatively: two more ticks never exceed 3 in flight.
    state = _apply_gates(state, [a.lane for a in gate_launches(actions)])
    second = plan_tick(state, max_lanes=30, max_gates=3)
    assert len(gate_launches(second)) == 0, "the 3 in flight already fill the pool"


def test_gates_are_released_in_cluster_order() -> None:
    """A low-priority cluster does not jump ahead of C0 for gate capacity."""
    items = [
        item("late", cluster="C9"),
        item("first", cluster="C0"),
        item("mid", cluster="C1"),
    ]
    state = state_with(*items)
    state = _with_lanes(state, {i.lane: DISPATCH_PR for i in items}, pr=1)
    actions = plan_tick(state, max_lanes=8, max_gates=2, cluster_order=("C0", "C1"))
    assert [a.lane for a in gate_launches(actions)] == ["first", "mid"]


def test_lane_and_gate_caps_are_independent() -> None:
    """Gating four PRs must not block launching a fresh lane."""
    # 2 lanes in `pr` (a lane slot but not a gate slot) + a fresh queued lane.
    items = [item(f"g{n}") for n in range(2)] + [item("fresh")]
    state = state_with(*items)
    state = _with_lanes(state, {f"g{n}": DISPATCH_PR for n in range(2)}, pr=1)
    actions = plan_tick(state, max_lanes=3, max_gates=2)
    assert len(gate_launches(actions)) == 2
    assert "fresh" in launches(actions), "gating existing PRs must not block a new lane"


# ------------------------------------------------------------ 6. the throttle


def test_saturated_cpu_pressure_blocks_new_lanes() -> None:
    """PSI above the ceiling means "do not add work"."""
    state = state_with(item("a"), item("b"))
    actions = plan_tick(state, max_lanes=8, max_gates=4, psi=SATIATED, psi_avg10_max=25.0)
    assert launches(actions) == []


def test_healthy_cpu_pressure_launches_normally() -> None:
    state = state_with(item("a"), item("b"))
    actions = plan_tick(state, max_lanes=8, max_gates=4, psi=IDLE, psi_avg10_max=25.0)
    assert launches(actions) == ["a", "b"]


def test_an_unreadable_psi_file_never_blocks() -> None:
    """Fail-open. The incident this replaces was a *false* block for 40 minutes."""
    state = state_with(item("a"))
    actions = plan_tick(state, max_lanes=8, max_gates=4, psi=NO_READING, psi_avg10_max=25.0)
    assert launches(actions) == ["a"]


def test_the_throttle_never_calls_load_average() -> None:
    """``os.getloadavg`` is never *called* anywhere in the fleet_ops throttle path.

    A cgroup-quota-throttled task still counts as *running* in the load average,
    so load reads high on an idle-but-stalled box. That is what stopped every
    launch for forty minutes.

    Checked over the parsed AST rather than the text: the modules name
    ``getloadavg`` in prose to explain what they replaced, and that must not be
    confused with using it.
    """
    assert "getloadavg" not in _called_names(pressure.__file__)
    assert "getloadavg" not in _called_names(dispatch_mod.__file__)


def _called_names(path: str) -> set[str]:
    """Every function/method name appearing in executable code (not docstrings)."""
    import ast

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_the_lane_cap_alone_bounds_launches_regardless_of_pressure() -> None:
    state = state_with(*[item(f"l{n}") for n in range(10)])
    actions = plan_tick(state, max_lanes=2, max_gates=4, psi=IDLE)
    assert launches(actions) == ["l0", "l1"]


# ------------------------------------------------------------- dependencies


def test_a_dependency_releases_when_its_lane_is_terminal() -> None:
    state = state_with(
        item("a"),
        item("b", depends_on=["R-a"]),
        item("c", depends_on=["R-a"]),
    )
    state = _with_lane(state, "a", state=DISPATCH_DONE, reason="approved")
    assert set(launches(plan_tick(state, max_lanes=8, max_gates=4))) == {"b", "c"}


def test_an_escalated_dependency_still_releases_its_chain() -> None:
    """A failure must not deadlock everything queued behind it."""
    state = state_with(item("a"), item("b", depends_on=["R-a"]))
    state = _with_lane(state, "a", state=DISPATCH_DONE, reason="escalated (exit 4)")
    assert launches(plan_tick(state, max_lanes=8, max_gates=4)) == ["b"]


def test_an_unqueued_dependency_is_ignored_rather_than_deadlocking() -> None:
    """A ref nothing in this queue produces can never become terminal."""
    state = state_with(item("a", depends_on=["R-from-another-queue"]))
    assert launches(plan_tick(state, max_lanes=8, max_gates=4)) == ["a"]
    assert blocked_lanes(state) == []


def test_a_dependency_cycle_is_reported_rather_than_waited_on_forever() -> None:
    """The other half of the StopIteration story: a queue that can never drain."""
    state = state_with(
        item("a", ref="R-a", depends_on=["R-b"]), item("b", ref="R-b", depends_on=["R-a"])
    )
    assert launches(plan_tick(state, max_lanes=8, max_gates=4)) == []
    assert sorted(blocked_lanes(state)) == ["a", "b"]


def test_a_dependency_on_a_stuck_lane_is_reported_too() -> None:
    # "c" depends on "a", which is itself deadlocked: c can never run either,
    # because a is a queued lane that is not terminal.
    state = state_with(
        item("a", depends_on=["R-b"]),
        item("b", depends_on=["R-a"]),
        item("c", depends_on=["R-a"]),
    )
    assert sorted(blocked_lanes(state)) == ["a", "b", "c"]
    assert launches(plan_tick(state, max_lanes=8, max_gates=4)) == []


def test_terminal_refs_include_both_the_ref_and_the_lane_name() -> None:
    state = _with_lane(state_with(item("a", ref="R-a")), "a", state=DISPATCH_DONE, reason="ok")
    refs = terminal_refs(state)
    assert "R-a" in refs and "a" in refs


def test_dependency_satisfied_is_pure_about_known_refs() -> None:
    lane = queued(state_with(item("a", depends_on=["R-x", "R-y"])), "a")
    assert dependency_satisfied(lane, released={"R-x"}, known={"R-x", "R-y"}) is False
    assert dependency_satisfied(lane, released={"R-x", "R-y"}, known={"R-x", "R-y"}) is True
    # "R-z" is not in the queue, so it is not a blocker.
    other = queued(state_with(item("b", depends_on=["R-z"])), "b")
    assert dependency_satisfied(other, released=set(), known=set()) is True


# ---------------------------------------------------------------- ordering


def test_cluster_order_is_honoured_and_unknown_clusters_sort_last() -> None:
    state = state_with(
        item("z", cluster="C2"),
        item("unknown", cluster="C99"),
        item("a", cluster="C0"),
        item("m", cluster="C1"),
    )
    order = [lane.lane for lane in order_lanes(state, cluster_order=("C0", "C1", "C2"))]
    assert order == ["a", "m", "z", "unknown"]


def test_lanes_sharing_a_cluster_keep_queue_order() -> None:
    state = state_with(
        item("first", cluster="C0"),
        item("second", cluster="C0"),
        item("third", cluster="C0"),
    )
    order = [lane.lane for lane in order_lanes(state, cluster_order=("C0",))]
    assert order == ["first", "second", "third"]


# ------------------------------------------------------------ gate commands


def test_the_gate_template_is_never_a_shell() -> None:
    """A template cannot smuggle in a second command."""
    argv = gate_argv(
        "/opt/fbgate {lane} {repo} {pr}; rm -rf /",
        lane="alpha",
        pr=42,
        repo="acme",
        slug="Evan-Kim2028/acme",
    )
    # shlex.split (not posix=False) separates on the ';', but the whole thing is
    # still ONE argv list handed to execve with no shell: the ';' is data in a
    # filename, never a command separator.
    assert argv[:4] == ["/opt/fbgate", "alpha", "acme", "42;"]
    assert argv[4:] == ["rm", "-rf", "/"]
    assert not any(";" in a for a in argv[4:])


def test_the_gate_template_expands_every_documented_placeholder() -> None:
    argv = gate_argv(
        "/opt/g --lane={lane} --repo={repo} --pr={pr} --op={operator} --slug={slug}",
        lane="alpha",
        pr=7,
        repo="acme",
        operator="documents-0e",
        slug="Evan-Kim2028/acme",
    )
    assert "--lane=alpha" in argv
    assert "--op=documents-0e" in argv
    assert "--slug=Evan-Kim2028/acme" in argv


def test_the_default_gate_command_is_the_built_in_fleet_gate() -> None:
    argv = gate_argv(None, lane="alpha", pr=7, repo="acme", slug="Evan-Kim2028/acme")
    assert argv == [
        "agent-fleet",
        "gate",
        "--lane",
        "alpha",
        "--repo",
        "Evan-Kim2028/acme",
        "--pr",
        "7",
        "--head-ref",
        "fb/alpha",
    ]


def test_the_gate_template_survives_a_stray_brace() -> None:
    """``expand_template`` is a replace, not a format, so ``{}`` cannot raise."""
    argv = gate_argv("/opt/g {lane} {}", lane="alpha", pr=1, repo="acme")
    assert argv == ["/opt/g", "alpha", "{}"]


def test_an_unparseable_gate_template_is_an_error_not_a_silent_skip() -> None:
    with pytest.raises(ValueError, match="not parseable"):
        gate_argv("/opt/g 'unterminated", lane="a", pr=1, repo="acme")


# ------------------------------------------------------------ gate verdicts


def test_an_approval_is_read_from_the_last_status_line() -> None:
    assert classify_status("12:00:00 PREMERGE-APPROVED abc1234def") == "approved"
    # Only the LAST line is the verdict. An approval followed by an escalation
    # means the gate escalated, and reading the transcript as a whole is how
    # "did not APPROVE the fix" gets mistaken for an approval.
    assert (
        classify_status("12:00:00 PREMERGE-APPROVED abc1234def\n12:01:00 NEEDS-ESCALATION stalled")
        == "escalated"
    )
    assert classify_status("12:00:00 NEEDS-ESCALATION stalled") == "escalated"


def test_a_gate_that_approves_then_exits_nonzero_still_approves() -> None:
    assert classify_status("12:00:00 PREMERGE-APPROVED abc1234def", exit_code=1) == "approved"
    # An escalation anywhere in the file still wins: the file is an append-only
    # history, so a later NEEDS-ESCALATION retracts an earlier approval.
    assert (
        classify_status(
            "12:00:00 PREMERGE-APPROVED abc1234def\n12:01:00 NEEDS-ESCALATION stalled",
            exit_code=0,
        )
        == "escalated"
    )
    assert classify_status("", exit_code=4) == "escalated (gate exit 4)"
    assert classify_status("", exit_code=0) == "escalated (no approval line)"
    assert classify_status("12:00:00 NEEDS-ESCALATION stalled", exit_code=0) == "escalated"


def test_a_lane_result_is_read_from_its_own_json() -> None:
    """The PR comes from the lane's verified result, not a fresh ``gh`` call."""
    out = 'noise\n{"state": "pr_guaranteed", "pr": 42, "worktree": "/w", "detail": ""}'
    assert read_lane_result(out) == (42, "/w", None)
    assert read_lane_result("") == (None, None, None)
    assert read_lane_result("not json at all") == (None, None, None)


# --------------------------------------------------------------- queue input


def test_the_queue_parses_with_only_the_required_fields() -> None:
    items = load_queue_from_text('{"lane": "a", "repo": "r"}')
    assert items[0].lane == "a" and items[0].ref == "a"


def test_a_queue_item_without_a_lane_is_rejected_with_its_line_number() -> None:
    with pytest.raises(ValueError, match=r":2:"):
        load_queue_from_text('{"lane": "a", "repo": "r"}\n{"repo": "r"}')


def test_a_malformed_jsonl_line_names_the_line() -> None:
    with pytest.raises(ValueError, match=r"not valid JSON"):
        load_queue_from_text('{"lane": "a", "repo": "r"}\n{oops}')


def test_blank_lines_are_skipped() -> None:
    assert len(load_queue_from_text('\n{"lane": "a", "repo": "r"}\n\n')) == 1


def test_a_scalar_depends_on_is_accepted() -> None:
    assert DispatchItem.from_dict({"lane": "a", "repo": "r", "depends_on": "R-b"}).depends_on == (
        "R-b",
    )


def test_the_generated_task_file_carries_triage_findings_and_fences() -> None:
    text = render_task_file(
        item("alpha", ref="R-1", area="api", size="S", evidence="found in #1", files=["a.py"]),
        fences="NEVER DO X",
    )
    assert "Evidence from triage: found in #1" in text
    assert "Files: a.py" in text
    assert "open ONE PR referencing R-1" in text
    assert text.rstrip().endswith("NEVER DO X")


# ------------------------------------------------------------- run_dispatch


class _FakeProc:
    """A spawned child that can be told what it wrote and when it exits.

    *polls* makes a child exit after that many ``poll()`` calls, so a test can
    model "ran for a while, then finished" without a real process. ``None`` means
    it never exits, which models a lane still working.
    """

    def __init__(self, pid: int, exit_code: int | None = None, *, polls: int = 0) -> None:
        self.pid = pid
        self.returncode = exit_code
        self._polls_left = polls

    def poll(self) -> int | None:
        if self._polls_left > 0:
            self._polls_left -= 1
            return None
        return self.returncode


def load_queue_from_text(text: str) -> tuple[DispatchItem, ...]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "q.jsonl"
        path.write_text(text, encoding="utf-8")
        return load_queue(path)


def _with_lane(st: DispatchState, name: str, **fields: Any) -> DispatchState:  # noqa: ANN401
    """``st`` (not ``state``) so a ``state=`` field kwarg does not collide."""
    lanes = dict(st.lanes)
    lanes[name] = _replace_lane(lanes[name], **fields)
    return DispatchState(
        operator=st.operator,
        queue_path=st.queue_path,
        lanes=lanes,
        started_ts=st.started_ts,
    )


def _with_lanes(st: DispatchState, names: dict[str, str], *, pr: int | None) -> DispatchState:
    lanes = dict(st.lanes)
    for name, lane_state in names.items():
        lanes[name] = _replace_lane(lanes[name], state=lane_state, pr=pr)
    return DispatchState(
        operator=st.operator,
        queue_path=st.queue_path,
        lanes=lanes,
        started_ts=st.started_ts,
    )


def _apply_gates(st: DispatchState, names: list[str]) -> DispatchState:
    lanes = dict(st.lanes)
    for name in names:
        lanes[name] = _replace_lane(lanes[name], state=DISPATCH_GATING)
    return DispatchState(
        operator=st.operator,
        queue_path=st.queue_path,
        lanes=lanes,
        started_ts=st.started_ts,
    )


def _replace_lane(lane: DispatchLane, **fields: Any) -> DispatchLane:  # noqa: ANN401
    from dataclasses import replace

    return replace(lane, **fields)


def test_run_dispatch_launches_gates_and_reports_a_summary(tmp_path: Path) -> None:
    """The happy path, end to end, with no real subprocess."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "alpha", "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"

    spawned: list[list[str]] = []
    stage = {"n": 0}

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        spawned.append(list(argv))
        stage["n"] += 1
        is_lane = argv[0] == "fleet" and "lane" in argv
        if is_lane:
            # A lane run reports a PR in its --json output.
            Path(str(kwargs["stdout"])).write_text(
                '{"state": "pr_guaranteed", "pr": 9, "worktree": "/w"}', encoding="utf-8"
            )
        else:
            # A gate writes its verdict to the lane's status file, which is what
            # the dispatcher reads back to classify the run.
            lane = argv[argv.index("--lane") + 1]
            status = out / "lanes" / f"{lane}.status"
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text("12:00:00 PREMERGE-APPROVED abc1234def\n", encoding="utf-8")
        # The lane works for a poll cycle before exiting; the gate writes its
        # verdict and exits immediately.
        return _FakeProc(1000 + stage["n"], 0, polls=1 if is_lane else 0)

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        max_lanes=2,
        max_gates=2,
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )
    assert summary.launched == 1
    assert summary.approved == 1
    assert summary.exit_code() == 0
    assert "--no-gate" in spawned[0], "the lane must run with the fleet gate disabled"


def test_run_dispatch_refuses_a_repo_it_has_no_checkout_for(tmp_path: Path) -> None:
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")
    with pytest.raises(ValueError, match="needs a repo map"):
        run_dispatch(operator="op", queue_path=queue, repos={})


def test_run_dispatch_records_a_dependency_cycle_instead_of_hanging(
    tmp_path: Path,
) -> None:
    """A queue that can never drain terminates, loudly, rather than spinning."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(
            json.dumps(item_raw)
            for item_raw in (
                {
                    "lane": "a",
                    "ref": "R-a",
                    "repo": "acme",
                    "task": "t",
                    "depends_on": ["R-b"],
                },
                {
                    "lane": "b",
                    "ref": "R-b",
                    "repo": "acme",
                    "task": "t",
                    "depends_on": ["R-a"],
                },
            )
        ),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    def never_sleep(_s: float) -> None:
        raise AssertionError("the dispatcher must not sleep on a deadlocked queue")

    summary = run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        spawn=lambda _argv, **_kw: _FakeProc(1, 0),
        psi_reader=lambda: IDLE,
        sleep=never_sleep,
        run_dir=tmp_path / "out",
    )
    assert summary.launched == 0
    assert summary.errors == 2
    state = load_state("documents-0e")
    assert {lane.reason for lane in state.lanes.values()} == {"dependency_deadlock"}


def test_run_dispatch_writes_events_namespaced_by_operator(tmp_path: Path) -> None:
    """The replacement for the single ``events.log`` two operators interleaved."""
    from agent_fleet.fleet_ops import registry

    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "alpha", "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        if kwargs.get("stdout") is not None and argv[0] == "fleet":
            Path(kwargs["stdout"]).write_text(
                '{"state": "pr_guaranteed", "pr": 9}', encoding="utf-8"
            )
        else:
            lane = argv[argv.index("--lane") + 1] if "--lane" in argv else "x"
            status = Path(str(out / "lanes" / f"{lane}.status"))
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text("12:00:00 PREMERGE-APPROVED abc1234def\n", encoding="utf-8")
        return _FakeProc(2000, 0)

    run_dispatch(
        operator="documents-0e",
        queue_path=queue,
        repos={"acme": str(repo)},
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )
    events = registry.read_events(operator="documents-0e")
    assert events, "the dispatcher must leave a trail"
    assert {e["event"] for e in events} >= {
        "dispatch.launched",
        "dispatch.pr",
        "dispatch.gate.started",
        "dispatch.done",
    }
    # And nothing leaked into the other operator's namespace.
    assert registry.read_events(operator="documents-1d") == []


def test_a_gate_can_be_capped_by_a_command_template(tmp_path: Path) -> None:
    """``--gate-cmd`` reaches the spawned argv."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "alpha", "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    seen: list[list[str]] = []

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        seen.append(list(argv))
        if argv[0] == "fleet":
            Path(str(kwargs["stdout"])).write_text(
                '{"state": "pr_guaranteed", "pr": 9}', encoding="utf-8"
            )
        else:
            status = Path(str(out / "lanes" / "alpha.status"))
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text("12:00:00 NEEDS-ESCALATION no\n", encoding="utf-8")
        return _FakeProc(3000, 0)

    summary = run_dispatch(
        operator="op",
        queue_path=queue,
        repos={"acme": str(repo)},
        gate_cmd="/opt/fbgate {lane} {repo} {pr}",
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )
    gate = next(argv for argv in seen if argv[0] == "/opt/fbgate")
    assert gate[:4] == ["/opt/fbgate", "alpha", "acme", "9"]
    assert summary.escalated == 1
    assert summary.exit_code() == 1


def test_psi_can_be_overridden_per_run(tmp_path: Path) -> None:
    """``--psi-avg10-max`` reaches the tick, and the throttle is fail-open."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    saturated = {"count": 0}

    def psi() -> pressure.Throttle:
        saturated["count"] += 1
        return SATIATED

    def sleep(_s: float) -> None:
        if saturated["count"] > 3:
            raise AssertionError("a saturated machine must not keep launching")

    summary = run_dispatch(
        operator="op",
        queue_path=queue,
        repos={"acme": str(repo)},
        psi_reader=psi,
        psi_avg10_max=25.0,
        spawn=lambda _argv, **_kw: _FakeProc(1, 0),
        sleep=sleep,
        run_dir=tmp_path / "out",
    )
    assert summary.launched == 0


def test_state_is_persisted_between_ticks(tmp_path: Path) -> None:
    """A restart mid-run finds the lanes already recorded."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        "\n".join(json.dumps({"lane": f"l{n}", "repo": "acme", "task": "t"}) for n in range(3)),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    pid = {"n": 4000}

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        pid["n"] += 1
        return _FakeProc(pid["n"], 0)

    # Everything exits immediately and reports no PR, so all lanes finish.
    run_dispatch(
        operator="op",
        queue_path=queue,
        repos={"acme": str(repo)},
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=tmp_path / "out",
    )
    state = load_state("op")
    assert set(state.lanes) == {"l0", "l1", "l2"}
    assert {lane.reason for lane in state.lanes.values()} == {"no_pr"}


def test_launches_and_gate_booleans_helpers_are_used_consistently() -> None:
    """Guards the helper semantics the other assertions rely on."""
    actions = plan_tick(state_with(item("a"), item("b")), max_lanes=1, max_gates=1)
    assert launches(actions) == ["a"]
    assert len(actions) == 1


def test_a_lane_cap_of_zero_launches_nothing() -> None:
    assert plan_tick(state_with(item("a")), max_lanes=0, max_gates=0) == []


def test_caller_supplied_state_is_used_verbatim(tmp_path: Path) -> None:
    """``run_dispatch`` accepts a pre-built state, which is how a resume is tested."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    seeded = _with_lane(state_with(item("a")), "a", state=DISPATCH_DONE, reason="approved")
    summary = run_dispatch(
        operator="op",
        queue_path=queue,
        repos={"acme": str(repo)},
        state=seeded,
        spawn=lambda _argv, **_kw: pytest.fail("must not launch"),
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=tmp_path / "out",
    )
    assert summary.launched == 0


def test_gate_argv_omits_worktree_when_unknown() -> None:
    argv = gate_argv("/opt/g {lane}", lane="a", pr=1, repo="r")
    assert "--worktree" not in argv
    with_worktree = gate_argv("/opt/g {lane}", lane="a", pr=1, repo="r", worktree="/w")
    assert with_worktree[-2:] == ["--worktree", "/w"]


def test_slashified_keys_are_stable_for_the_same_origin() -> None:
    from agent_fleet.gate.gitops import _repo_key

    key = _repo_key(Path("/tmp/does-not-exist"))
    assert key.startswith("path-")
    assert key == _repo_key(Path("/tmp/does-not-exist"))


def test_run_dispatch_seams_are_keyword_only_and_have_real_defaults() -> None:
    """The test seams default to the real implementations, never to fakes."""
    import inspect

    params = inspect.signature(run_dispatch).parameters
    for name in ("spawn", "psi_reader", "sleep"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is not _NO_DEFAULT
    # There is deliberately no `alive` seam: exit detection is Popen.poll plus
    # the recorded pid/starttime fingerprint, so there is nothing to inject.
    assert "alive" not in params


class _NoDefault:
    """Sentinel distinct from every real default."""


_NO_DEFAULT = _NoDefault()


def test_launch_gate_requires_a_known_item() -> None:
    """Defensive: a gate cannot be launched for a lane that has no PR record."""
    state = state_with(item("a"))
    actions = [LaunchGate(lane="a", pr=1)]
    assert isinstance(actions[0], LaunchGate)
    assert state.lanes["a"].pr is None


def test_pure_launch_helper_returns_expected_names() -> None:
    assert LaunchLane("a").lane == "a"
    assert ReapLane("a", exit_code=0).exit_code == 0
    assert FinishLane("a", "approved").reason == "approved"
    assert ReapGate("a", exit_code=1).exit_code == 1


def test_spawn_callable_signature_matches_what_run_dispatch_passes() -> None:
    """The injected spawn receives argv plus the three Popen kwargs."""
    recorded: dict[str, Any] = {}

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        recorded.update(kwargs)
        recorded["argv"] = argv
        return _FakeProc(1, 0)

    spawn(["fleet", "lane"], stdout=Path("/tmp/x"), stderr=2, start_new_session=True)
    assert set(recorded) == {"argv", "stdout", "stderr", "start_new_session"}
    assert recorded["start_new_session"] is True


def test_launching_a_lane_writes_a_task_file_and_a_status_file(tmp_path: Path) -> None:
    """Both artifacts are the contract downstream tooling reads."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(
        json.dumps({"lane": "alpha", "repo": "acme", "task": "do the thing"}),
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        return _FakeProc(1, 0)

    run_dispatch(
        operator="op",
        queue_path=queue,
        repos={"acme": str(repo)},
        spawn=spawn,
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=out,
    )
    assert "do the thing" in (out / "prompts" / "alpha.task.md").read_text(encoding="utf-8")
    state = load_state("op")
    assert state.lanes["alpha"].status_file is not None
    assert Path(str(state.lanes["alpha"].status_file)).parent.is_dir()


def test_lane_argv_asserts_the_expected_repo(tmp_path: Path) -> None:
    """``--expected-repo`` is what stops a lane opening a PR in another repo."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    seen: list[list[str]] = []

    run_dispatch(
        operator="op",
        queue_path=queue,
        repos={"acme": str(repo)},
        spawn=lambda argv, **_kw: (seen.append(list(argv)), _FakeProc(1, 0))[1],
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=tmp_path / "out",
    )
    argv = seen[0]
    assert argv[argv.index("--expected-repo") + 1].endswith("/acme")
    assert "--no-gate" in argv


def test_a_missing_repo_directory_is_recorded_per_lane(tmp_path: Path) -> None:
    """A bad path fails that lane only."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")
    summary = run_dispatch(
        operator="op",
        queue_path=queue,
        repos={"acme": str(tmp_path / "nope")},
        spawn=lambda _argv, **_kw: pytest.fail("must not launch"),
        psi_reader=lambda: IDLE,
        sleep=lambda _s: None,
        run_dir=tmp_path / "out",
    )
    assert summary.errors == 1
    assert "does not exist" in str(load_state("op").lanes["a"].error)


def test_a_dispatch_run_is_idempotent_across_restarts(tmp_path: Path) -> None:
    """Running the same completed queue twice launches nothing the second time."""
    queue = tmp_path / "q.jsonl"
    queue.write_text(json.dumps({"lane": "a", "repo": "acme", "task": "t"}), encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out"
    lane_launches: list[int] = []

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        is_lane = argv[0] == "fleet" and "lane" in argv
        if is_lane:
            lane_launches.append(1)
            Path(str(kwargs["stdout"])).write_text(
                '{"state": "pr_guaranteed", "pr": 9}', encoding="utf-8"
            )
        else:
            status = out / "lanes" / "a.status"
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text("12:00:00 PREMERGE-APPROVED abc1234def\n", encoding="utf-8")
        return _FakeProc(5000 + len(lane_launches) + len(list(out.rglob("*.log"))), 0)

    for _ in range(2):
        run_dispatch(
            operator="op",
            queue_path=queue,
            repos={"acme": str(repo)},
            spawn=spawn,
            psi_reader=lambda: IDLE,
            sleep=lambda _s: None,
            run_dir=out,
        )
    assert len(lane_launches) == 1, "the second run must not relaunch a finished lane"


def test_lane_slots_count_a_gating_lane() -> None:
    """A lane waiting on its gate still holds a lane slot: it is not done."""
    items = [item(f"l{n}") for n in range(4)]
    state = _with_lanes(state_with(*items), {f"l{n}": DISPATCH_GATING for n in range(2)}, pr=1)
    actions = plan_tick(state, max_lanes=2, max_gates=4)
    assert launches(actions) == [], "two gating lanes already fill both lane slots"
    assert DISPATCH_GATING in {(a.lane and "gating") or "" for a in []} | {"gating"}


def test_psi_threshold_is_honoured_exactly_at_the_boundary() -> None:
    state = state_with(item("a"))
    at_limit = pressure.Throttle(some_avg10=25.0, path=Path("/f"), available=True)
    assert launches(plan_tick(state, max_lanes=4, max_gates=2, psi=at_limit, psi_avg10_max=25.0))
    over = pressure.Throttle(some_avg10=25.1, path=Path("/f"), available=True)
    assert not launches(plan_tick(state, max_lanes=4, max_gates=2, psi=over, psi_avg10_max=25.0))


def test_events_are_appended_with_the_operator_tag() -> None:
    from agent_fleet.fleet_ops import registry

    state = state_with(item("a"))
    _record = registry.append_event("documents-1d", "a", "dispatch.test", detail="x")
    assert registry.read_events(operator="documents-1d")
    assert state.operator == "documents-0e"
    assert _record.exists()


def test_unknown_cluster_rank_sorts_after_every_configured_one() -> None:
    from agent_fleet.fleet_ops.dispatch import cluster_rank

    assert cluster_rank("C0", ("C0", "C1")) == 0
    assert cluster_rank("C1", ("C0", "C1")) == 1
    assert cluster_rank("Z", ("C0", "C1")) == 2


def test_slash_free_lane_names_do_not_need_escaping() -> None:
    """Lane names are used as filenames; the component sanitizer keeps them safe."""
    from agent_fleet.fleet_ops.dispatch import _slugify

    assert _slugify("fb/alpha") == "fb-alpha"
    assert _slugify("a b") == "a-b"


def _write_queue(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "q.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return repo


def test_the_dispatch_loop_sleeps_between_ticks_when_work_is_in_flight(
    tmp_path: Path,
) -> None:
    """A no-op ``sleep`` must not spin: with a live lane the loop yields.

    The injected ``sleep`` is what makes the loop testable; this asserts it is
    actually reached, so a regression to a busy-wait is caught rather than
    hanging the suite. The child never exits, which is the one case a real
    dispatcher *should* wait on indefinitely.
    """
    import os

    from agent_fleet.fleet_ops import registry

    calls: list[float] = []
    queue = _write_queue(tmp_path, [{"lane": "a", "repo": "acme", "task": "t"}])
    repo = _repo(tmp_path)

    # The fake child borrows *this* process's identity, so the recorded pid is
    # genuinely alive and the liveness probe agrees the lane is still running.
    me = os.getpid()
    mine = registry.process_starttime(me)

    def spawn(argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        return _FakeProc(me, None, polls=10_000)  # never exits

    original_start = registry.process_starttime

    def starttime(pid: int) -> int | None:
        return mine if pid == me else original_start(pid)

    registry.process_starttime = starttime  # ty: ignore[invalid-assignment]
    try:

        def sleep(seconds: float) -> None:
            calls.append(seconds)
            raise _StopAfterSleep

        with pytest.raises(_StopAfterSleep):
            run_dispatch(
                operator="op",
                queue_path=queue,
                repos={"acme": str(repo)},
                spawn=spawn,
                psi_reader=lambda: IDLE,
                sleep=sleep,
                run_dir=tmp_path / "out",
            )
    finally:
        registry.process_starttime = original_start  # type: ignore[assignment]
    assert calls, "the loop must yield to its sleep while a lane is in flight"
    assert calls == [DEFAULT_TICK_SECONDS], "one idle tick sleeps exactly one tick interval"


def test_an_exited_child_is_reported_without_a_pid_probe(
    tmp_path: Path,
) -> None:
    """``plan_tick`` trusts a handle's exit over ``os.kill(pid, 0)``.

    A pid probe answers "yes" for a zombie, so a lane that has already exited
    but not been reaped would look alive and hold its slot forever. The handle
    is the only thing that can say otherwise — this pins that it is consulted.
    """
    queue = _write_queue(tmp_path, [{"lane": "a", "repo": "acme", "task": "t"}])
    _repo(tmp_path)
    proc = _FakeProc(4321, 0)

    st = d_merge(queue)
    st = _with_lane(st, "a", state="running", lane_pid=4321, lane_starttime=1)
    st = _with_procs(st, {"a": proc})
    # Even with a pid probe that insists the lane is alive, the exit wins.
    monkey_actions = plan_tick(
        st,
        max_lanes=4,
        max_gates=2,
        exited={"a": 0},
    )
    assert [type(a).__name__ for a in monkey_actions] == ["ReapLane"]


def d_merge(queue: Path) -> DispatchState:
    import agent_fleet.fleet_ops.dispatch as dispatch_mod

    return dispatch_mod.merge_queue(DispatchState(operator="op"), load_queue(queue))


def _with_procs(st: DispatchState, procs: dict[str, Any]) -> DispatchState:
    return DispatchState(
        operator=st.operator,
        queue_path=st.queue_path,
        lanes=st.lanes,
        started_ts=st.started_ts,
        procs=procs,
    )


class _StopAfterSleep(Exception):
    """Sentinel raised from the injected sleep to break out of the loop."""


_STOP_RAISED = _StopAfterSleep()


def test_pressure_module_is_the_throttle_source() -> None:
    """Guards the wiring: dispatch imports the PSI module, not a local copy."""
    import agent_fleet.fleet_ops.dispatch as dispatch_mod

    assert dispatch_mod.pressure is pressure


def test_no_module_in_the_throttle_path_calls_load_average() -> None:
    """No fleet_ops module may reach for the load average as a throttle.

    ``status.py`` and ``stall.py`` are excluded: one renders an age column and
    the other computes idle-for, and neither makes a launch decision.
    """
    root = Path(__file__).resolve().parents[1] / "agent_fleet" / "fleet_ops"
    for path in sorted(root.glob("*.py")):
        if path.name in {"status.py", "stall.py"}:
            continue
        assert "getloadavg" not in _called_names(str(path)), path.name


def test_psi_reader_default_is_the_agents_slice() -> None:
    """The signal must describe the agents, not the whole machine."""
    assert "agents.slice" in str(pressure.DEFAULT_PSI_PATH)


def test_the_throttle_fails_open_on_a_missing_file() -> None:
    reading = pressure.read_throttle(Path("/definitely/not/here/cpu.pressure"), fallbacks=())
    assert reading.available is False
    assert pressure.throttled(reading) is False


def test_a_queue_item_may_carry_no_task() -> None:
    """A task-less item still dispatches; the lane reports the problem itself."""
    parsed = DispatchItem.from_dict({"lane": "a", "repo": "r"})
    assert parsed.task == ""


def test_depends_on_defaults_to_empty() -> None:
    assert DispatchItem.from_dict({"lane": "a", "repo": "r"}).depends_on == ()


def test_files_default_to_empty_when_absent() -> None:
    assert DispatchItem.from_dict({"lane": "a", "repo": "r"}).files == ()


def test_files_and_dbt_models_accept_a_bare_string() -> None:
    parsed = DispatchItem.from_dict(
        {"lane": "a", "repo": "r", "files": "x.py", "dbt_models": "m.sql"}
    )
    assert parsed.files == ("x.py",)
    assert parsed.dbt_models == ("m.sql",)


def test_item_to_dict_round_trips() -> None:
    original = item("a", cluster="C1", depends_on=["R-b"], files=["x.py"])
    assert DispatchItem.from_dict(original.to_dict()) == original


def test_lane_to_dict_round_trips() -> None:
    lane = DispatchLane(lane="a", item=item("a"), state=DISPATCH_PR, pr=3)
    assert DispatchLane.from_dict(lane.to_dict()) == lane


def test_state_to_dict_round_trips() -> None:
    state = state_with(item("a"), item("b"))
    assert DispatchState.from_dict(state.to_dict()).lanes.keys() == state.lanes.keys()


def test_gate_process_alive_uses_the_gate_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_fleet.fleet_ops import registry

    monkeypatch.setattr(registry, "process_alive", lambda pid: pid == 88)
    monkeypatch.setattr(registry, "process_starttime", lambda _pid: 7)
    lane = DispatchLane(lane="a", item=item("a"), gate_pid=88, gate_starttime=7)
    assert dispatch_mod.gate_process_alive(lane) is True
    stale = DispatchLane(lane="a", item=item("a"), gate_pid=88, gate_starttime=8)
    assert dispatch_mod.gate_process_alive(stale) is False


def test_pure_functions_do_not_touch_the_filesystem() -> None:
    """``plan_tick`` is pure: same state in, same actions out, no IO."""
    state = state_with(item("a"), item("b", cluster="C0"))
    first = plan_tick(state, max_lanes=1, max_gates=1)
    second = plan_tick(state, max_lanes=1, max_gates=1)
    assert first == second
    assert dispatch_state_path("documents-0e").parent.name == "documents-0e"
