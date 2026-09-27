"""Escalation routing, the item board, the status screen and the serve loop.

The routing table is the interesting part of escalation: the gate emits a
free-text ``NEEDS-ESCALATION <reason>`` and no reason class exists anywhere in
the codebase, so the taxonomy and its parser are defined here. The property
that matters most is the default — an unrecognised reason must go to a human,
never to an automatic retry, because guessing ``infra`` would mean
automatically re-running something a human deliberately fenced.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.capacity import CapacityBounds, CapacityTargets
from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.config import (
    ServeConfig,
    ServeConfigError,
    load_serve_config,
    parse_serve_config,
)
from agent_fleet.serve.escalate import (
    ACTION_DECIDE,
    ACTION_FIX_ROUND,
    ACTION_RETRY,
    CLASS_FENCE,
    CLASS_INFRA,
    CLASS_OWNER,
    CLASS_UNKNOWN,
    CLASS_UNTESTABLE,
    EscalationRouter,
    action_for,
    classify_reason,
)
from agent_fleet.serve.items import (
    STAGE_APPROVED,
    STAGE_ESCALATED,
    STAGE_GATING,
    STAGE_MERGED,
    STAGE_QUEUED,
    STAGE_RUNNING,
    STAGES,
    ItemBoard,
    stage_from_status_line,
)
from agent_fleet.serve.status import degraded_note, render_status, status_snapshot
from agent_fleet.serve.supervisor import Supervisor

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


# ------------------------------------------------------------------ classify


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("fenced: pipelines/foo/print_identity.py", CLASS_FENCE),
        ("blocked by a standing fence", CLASS_FENCE),
        ("needs an owner decision about the schema", CLASS_OWNER),
        ("ambiguous whether the scope includes docs", CLASS_OWNER),
        ("untestable: docs contradict the code", CLASS_UNTESTABLE),
        ("judge-confirmed defect with no test", CLASS_UNTESTABLE),
        ("gate tests could not run: INFRA missing dependency", CLASS_INFRA),
        ("cannot create worktree", CLASS_INFRA),
        ("git fetch timed out", CLASS_INFRA),
        ("", CLASS_UNKNOWN),
        ("something nobody has ever written before", CLASS_UNKNOWN),
    ],
)
def test_classify_reason(reason: str, expected: str) -> None:
    assert classify_reason(reason) == expected


def test_a_fence_marker_beats_an_infra_marker() -> None:
    """A fenced item that also mentions a test run is still fenced.

    The marker order is the precedence, and it is precedence that matters: a
    fence that happened to trip while pytest was starting must not be routed as
    a transient infrastructure blip and retried.
    """
    reason = "fenced file could not run: the owner fence forbids editing print_identity.py"
    assert classify_reason(reason) == CLASS_FENCE


def test_an_explicit_class_token_is_authoritative() -> None:
    assert classify_reason("class=owner_decision which way round") == CLASS_OWNER
    assert classify_reason("[class: untestable] a docs bug") == CLASS_UNTESTABLE
    assert classify_reason("class=owner, please advise") == CLASS_OWNER


def test_an_unknown_explicit_token_falls_through_to_marker_matching() -> None:
    assert classify_reason("class=meteorology: gate could not run") == CLASS_INFRA


# ------------------------------------------------------------------- routing


def test_infra_retries_once_then_goes_to_a_human() -> None:
    """One retry: a second failure of the same kind means the machine is broken."""
    assert action_for(CLASS_INFRA, attempts=0) == ACTION_RETRY
    assert action_for(CLASS_INFRA, attempts=1) == ACTION_DECIDE


def test_untestable_goes_to_a_fix_round() -> None:
    assert action_for(CLASS_UNTESTABLE, attempts=0) == ACTION_FIX_ROUND
    assert action_for(CLASS_UNTESTABLE, attempts=5) == ACTION_FIX_ROUND


def test_fence_and_owner_always_go_to_a_human_never_a_retry() -> None:
    for attempts in range(3):
        assert action_for(CLASS_FENCE, attempts=attempts) == ACTION_DECIDE
        assert action_for(CLASS_OWNER, attempts=attempts) == ACTION_DECIDE


def test_unknown_reasons_go_to_a_human_never_a_retry() -> None:
    """The default that matters most.

    An unparsed reason is one this code has never seen. Treating it as infra
    would give it an automatic retry, which for a fence violation means
    re-running something a human deliberately stopped.
    """
    assert action_for(CLASS_UNKNOWN, attempts=0) == ACTION_DECIDE
    assert action_for(CLASS_UNKNOWN, attempts=9) == ACTION_DECIDE


def test_router_retries_infra_once_and_then_queues(tmp_path: Path) -> None:
    router = EscalationRouter("op", clock=FakeClock(), decisions_file=tmp_path / "d.jsonl")
    first = router.route("lane-1", "gate tests could not run: missing dependency")
    assert first.action == ACTION_RETRY
    assert first.queued is False
    second = router.route("lane-1", "gate tests could not run: missing dependency")
    assert second.action == ACTION_DECIDE
    assert second.queued is True


def test_router_queues_a_fence_without_retrying(tmp_path: Path) -> None:
    router = EscalationRouter("op", clock=FakeClock(), decisions_file=tmp_path / "d.jsonl")
    route = router.route("lane-2", "fenced: do not edit print_identity.py", pr=3541)
    assert route.reason_class == CLASS_FENCE
    assert route.action == ACTION_DECIDE
    assert route.queued is True
    pending = router.pending()
    assert [d.item_id for d in pending] == ["lane-2"]
    assert pending[0].pr == 3541


def test_router_never_retries_a_fence_however_many_times(tmp_path: Path) -> None:
    router = EscalationRouter("op", clock=FakeClock(), decisions_file=tmp_path / "d.jsonl")
    for _ in range(5):
        assert router.route("lane-3", "fenced").action == ACTION_DECIDE


def test_decisions_queue_is_append_only_and_resolvable(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    router = EscalationRouter("op", clock=FakeClock(), decisions_file=path)
    router.route("a", "fenced")
    router.route("b", "owner decision needed")
    assert len(router.pending()) == 2
    assert router.resolve("a", "unfenced, proceed") is True
    assert [d.item_id for d in router.pending()] == ["b"]
    assert router.resolve("never-raised", "x") is False
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3, "a human working the queue and serve must not lose each other's writes"
    assert json.loads(lines[-1])["resolved"] is True
    assert json.loads(lines[0])["item_id"] == "a", "history is never rewritten"


def test_decisions_file_uses_one_line_per_item(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    router = EscalationRouter("op", clock=FakeClock(), decisions_file=path)
    router.route("x" * 400, "owner decision", detail={"note": "y" * 400})
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_pending_of_a_missing_queue_is_empty(tmp_path: Path) -> None:
    router = EscalationRouter("op", clock=FakeClock(), decisions_file=tmp_path / "absent.jsonl")
    assert router.pending() == []


# ---------------------------------------------------------------- item board


def test_board_records_and_folds_transitions(tmp_path: Path) -> None:
    clock = FakeClock()
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("lane-1", STAGE_QUEUED, repo="lake")
    clock.advance(60)
    board.record("lane-1", STAGE_RUNNING, repo="lake")
    clock.advance(60)
    board.record("lane-1", STAGE_GATING, repo="lake")
    items = board.items()
    assert items["lane-1"].stage == STAGE_GATING
    assert items["lane-1"].entered_epoch == clock.time()
    assert items["lane-1"].first_seen_epoch == clock.time() - 120


def test_board_rejects_an_unknown_stage(tmp_path: Path) -> None:
    """A typo must not create a stage nothing counts.

    Otherwise the item silently vanishes from depth, throughput and the status
    screen, and the operator concludes the fleet is idle.
    """
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    assert board.record("lane-1", "gatingg") is None
    assert board.items() == {}


def test_board_survives_a_truncated_final_line(tmp_path: Path) -> None:
    path = tmp_path / "items.jsonl"
    board = ItemBoard(path, clock=FakeClock())
    board.record("lane-1", STAGE_QUEUED)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"item_id": "lane-2", "stage": "run')
    assert [i.item_id for i in board.items().values()] == ["lane-1"]


def test_board_depth_counts_every_stage(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    board.record("a", STAGE_QUEUED)
    board.record("b", STAGE_QUEUED)
    board.record("c", STAGE_GATING)
    depth = board.depth()
    assert depth[STAGE_QUEUED] == 2
    assert depth[STAGE_GATING] == 1
    assert set(depth) == set(STAGES)


def test_throughput_counts_entries_per_stage(tmp_path: Path) -> None:
    clock = FakeClock()
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    for i in range(3):
        board.record(f"lane-{i}", STAGE_MERGED)
    per_hour = board.throughput(window_hours=1.0, now=clock.time())
    assert per_hour[STAGE_MERGED] == 3.0


def test_throughput_window_excludes_older_transitions(tmp_path: Path) -> None:
    clock = FakeClock()
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("old", STAGE_MERGED)
    clock.advance(7200)
    board.record("new", STAGE_MERGED)
    per_hour = board.throughput(window_hours=1.0, now=clock.time())
    assert per_hour[STAGE_MERGED] == 1.0, "the two-hour-old entry is outside the window"


def test_throughput_of_a_zero_window_is_zero_not_a_division_error(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    board.record("a", STAGE_MERGED)
    assert board.throughput(window_hours=0.0)[STAGE_MERGED] == 0.0


def test_stage_stats_report_the_oldest_item_and_why_it_waits(tmp_path: Path) -> None:
    clock = FakeClock()
    board = ItemBoard(tmp_path / "items.jsonl", clock=clock)
    board.record("old", STAGE_QUEUED)
    clock.advance(1800)
    board.record("new", STAGE_QUEUED)
    stats = {s.stage: s for s in board.stage_stats(now=clock.time())}
    queued = stats[STAGE_QUEUED]
    assert queued.depth == 2
    assert queued.oldest is not None and queued.oldest.item_id == "old"
    assert queued.oldest_age_s == 1800
    assert "queued 30m" in queued.wait_reason


def test_a_components_own_wait_reason_wins(tmp_path: Path) -> None:
    """A lane that says why it is blocked knows something the controller does not."""
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    board.record("a", STAGE_QUEUED, wait_reason="blocked on depends_on lane-b")
    stats = {s.stage: s for s in board.stage_stats()}
    assert stats[STAGE_QUEUED].wait_reason == "blocked on depends_on lane-b"


def test_wait_reason_names_the_gate_limit(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    board.record("a", STAGE_GATING)
    targets = CapacityTargets(max_lanes=4, max_gates=3, test_pool=1, typecheck_pool=1)
    stats = {s.stage: s for s in board.stage_stats(targets=targets)}
    assert "max_gates=3" in stats[STAGE_GATING].wait_reason


def test_wait_reason_mentions_gates_priority(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    board.record("a", STAGE_QUEUED)
    targets = CapacityTargets(
        max_lanes=1, max_gates=3, test_pool=1, typecheck_pool=1, gates_priority=True
    )
    stats = {s.stage: s for s in board.stage_stats(targets=targets)}
    assert "gates_priority" in stats[STAGE_QUEUED].wait_reason


def test_an_empty_stage_has_no_wait_reason(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    stats = {s.stage: s for s in board.stage_stats()}
    assert stats[STAGE_GATING].wait_reason == ""
    assert stats[STAGE_GATING].oldest is None


@pytest.mark.parametrize(
    ("line", "stage"),
    [
        ("12:00:00 PREMERGE-APPROVED abc123def", STAGE_APPROVED),
        ("12:00:00 loop result: APPROVE deadbeef", STAGE_APPROVED),
        ("12:00:00 NEEDS-ESCALATION fail-closed", STAGE_ESCALATED),
        ("12:00:00 NEEDS-REBASE conflicting with main", STAGE_ESCALATED),
        ("12:00:00 start @abc123def (PR #12)", STAGE_GATING),
        ("12:00:00 gate: step0 PR tests", STAGE_GATING),
        ("12:00:00 something else entirely", STAGE_RUNNING),
        ("", None),
    ],
)
def test_status_lines_classify_into_stages(line: str, stage: str | None) -> None:
    """So serve can report on a fleet still running the old bash drivers."""
    assert stage_from_status_line(line) == stage


# -------------------------------------------------------------------- status


def _status(board: ItemBoard, operator: str = "op") -> dict:
    supervisor = Supervisor(operator, ServeConfig(operator=operator), clock=FakeClock())
    return status_snapshot(operator, supervisor, board, capacity_file=None)


def test_status_snapshot_carries_every_required_field(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    board.record("a", STAGE_QUEUED)
    snapshot = _status(board)
    assert snapshot["operator"] == "op"
    assert {"components", "up", "total", "crash_looping", "restarts"} <= set(snapshot["supervisor"])
    assert {"stage", "depth", "oldest_item", "oldest_age_s", "wait_reason", "per_hour"} <= set(
        snapshot["stages"][0]
    )
    assert snapshot["queue_depth"] >= 1


def test_status_without_a_capacity_file_says_so(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    snapshot = _status(board)
    assert snapshot["capacity"]["available"] is False
    rendered = render_status(snapshot)
    assert "no capacity file yet" in rendered
    assert "0.00" not in rendered.split("STAGES")[0], "a missing reading must not render as 0"


def test_status_renders_a_degraded_capacity_read_as_a_failure(tmp_path: Path) -> None:
    """The honesty property.

    Printing `cpu some-avg60 0.0` for a missing cgroup would be
    indistinguishable from an idle machine in the same field.
    """
    from agent_fleet.serve.capacity import write_capacity
    from agent_fleet.serve.paths import capacity_path
    from agent_fleet.serve.pressure import PressureReading

    operator = "op"
    targets = CapacityTargets(
        max_lanes=1, max_gates=1, test_pool=1, typecheck_pool=1, degraded=True
    )
    write_capacity(
        operator,
        targets,
        PressureReading(ok=False, error="cgroup 'agents.slice' not found"),
        clock=FakeClock(),
        path=capacity_path(operator),
    )
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    snapshot = _status(board, operator)
    assert snapshot["capacity"]["pressure_ok"] is False
    rendered = render_status(snapshot)
    assert "UNAVAILABLE" in rendered
    assert "not found" in rendered
    assert "DEGRADED" in rendered
    assert "pressure unavailable" in degraded_note(snapshot)


def test_status_renders_targets_against_pressure(tmp_path: Path) -> None:
    from agent_fleet.serve.capacity import write_capacity
    from agent_fleet.serve.paths import capacity_path
    from agent_fleet.serve.pressure import CpuPressure, PressureReading

    operator = "op"
    targets = CapacityTargets(
        max_lanes=6, max_gates=4, test_pool=2, typecheck_pool=2, signal="hold", reason="inside band"
    )
    write_capacity(
        operator,
        targets,
        PressureReading(
            ok=True,
            path="/sys/fs/cgroup/agents.slice",
            cpu=CpuPressure(some_avg60=12.0),
            memory_used_bytes=50,
            memory_max_bytes=100,
            memory_ratio=0.5,
        ),
        clock=FakeClock(),
        path=capacity_path(operator),
    )
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    rendered = render_status(_status(board, operator))
    assert "lanes 6" in rendered
    assert "some-avg60 12.0" in rendered
    assert "memory 50%" in rendered
    assert "/sys/fs/cgroup/agents.slice" in rendered


def test_status_flags_gates_priority(tmp_path: Path) -> None:
    from agent_fleet.serve.capacity import write_capacity
    from agent_fleet.serve.paths import capacity_path
    from agent_fleet.serve.pressure import PressureReading

    operator = "op"
    targets = CapacityTargets(
        max_lanes=1, max_gates=4, test_pool=1, typecheck_pool=1, gates_priority=True
    )
    write_capacity(
        operator, targets, PressureReading(ok=True), clock=FakeClock(), path=capacity_path(operator)
    )
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    rendered = render_status(_status(board, operator))
    assert "gates_priority ON" in rendered


def test_degraded_note_is_empty_when_all_is_well(tmp_path: Path) -> None:
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    assert degraded_note(_status(board)) == ""


def test_status_renders_a_crash_looping_component(tmp_path: Path) -> None:
    from agent_fleet.serve.supervisor import STATE_CRASH_LOOPING

    operator = "op"
    supervisor = Supervisor(operator, ServeConfig(operator=operator), clock=FakeClock())
    from agent_fleet.serve.supervisor import ChildState

    supervisor.children["dispatcher"] = ChildState(
        name="dispatcher", state=STATE_CRASH_LOOPING, message="5 crashes in 15m; not restarting"
    )
    board = ItemBoard(tmp_path / "items.jsonl", clock=FakeClock())
    snapshot = status_snapshot(operator, supervisor, board, capacity_file=None)
    rendered = render_status(snapshot)
    assert "crash-looping" in rendered
    assert "dispatcher" in degraded_note(snapshot)


# -------------------------------------------------------------------- config


def test_parse_serve_config_reads_the_nested_capacity_section() -> None:
    """The bug this pins: capacity was read from the serve root, so every
    configured floor and watermark was silently ignored."""
    config = parse_serve_config(
        {
            "tick_seconds": 5,
            "capacity": {
                "floors": {"lanes": 2, "gates": 1},
                "ceilings": {"lanes": 8, "gates": 4},
                "psi_low": 5,
                "psi_high": 20,
                "step": 3,
            },
            "components": {"dispatcher": {"command": "true"}},
        }
    )
    assert config is not None
    assert config.tick_seconds == 5
    assert config.bounds.lanes_floor == 2
    assert config.bounds.lanes_ceiling == 8
    assert config.marks.low == 5
    assert config.capacity.step == 3
    assert config.component("dispatcher").enabled is True
    assert config.component("merger").enabled is False


def test_parse_serve_config_reads_per_component_and_watchdog_timeouts() -> None:
    config = parse_serve_config(
        {
            "components": {
                "dispatcher": {
                    "command": "run",
                    "timeouts": {"crash": {"threshold": 2, "window_minutes": 5}},
                }
            },
            "watchdog": {
                "orphan_minutes": 30,
                "timeouts": {"gate": {"minutes": 15}},
                "remediation_budget": {"max_per_tick": 3},
            },
        }
    )
    assert config is not None
    dispatcher = config.component("dispatcher")
    assert dispatcher.crash_threshold == 2
    assert dispatcher.crash_window_minutes == 5
    assert config.watchdog.orphan_minutes == 30
    assert config.watchdog.timeout_for("gate") == 15
    assert config.watchdog.max_remediations_per_tick == 3


def test_defaults_are_safe_without_any_configuration() -> None:
    config = ServeConfig()
    assert config.bounds.lanes_floor >= 1
    assert config.watchdog.stage_retry_budget >= 1
    assert config.enabled_components == ()


def test_parse_serve_config_of_a_non_mapping_is_none() -> None:
    assert parse_serve_config("nope") is None  # type: ignore[arg-type]
    assert parse_serve_config(None) is None  # type: ignore[arg-type]


def test_load_serve_config_reads_a_named_file(tmp_path: Path) -> None:
    path = tmp_path / "fleet.yaml"
    path.write_text("serve:\n  tick_seconds: 42\n", encoding="utf-8")
    config = load_serve_config(operator="op", config_path=path, repo_root=tmp_path)
    assert config.tick_seconds == 42


def test_load_serve_config_accepts_a_repo_fleet_ops_serve_block(tmp_path: Path) -> None:
    """A repo may carry fleet_ops.serve, because that is where an operator
    looking at a repo will look for it."""
    path = tmp_path / ".agent-fleet.yaml"
    path.write_text(
        "fleet_ops:\n  serve:\n    tick_seconds: 7\n    cgroup: repo.slice\n", encoding="utf-8"
    )
    config = load_serve_config(operator="op", repo_root=tmp_path)
    assert config.tick_seconds == 7
    assert config.cgroup == "repo.slice"


def test_a_named_config_without_a_serve_section_is_an_error(tmp_path: Path) -> None:
    """Never a silent fall back to defaults.

    A supervisor quietly running on thresholds the operator believes they
    configured is the failure this rule prevents.
    """
    path = tmp_path / "fleet.yaml"
    path.write_text("default_model: composer\n", encoding="utf-8")
    with pytest.raises(ServeConfigError, match="no `serve:`"):
        load_serve_config(operator="op", config_path=path, repo_root=tmp_path)


def test_bounds_clamp_in_both_directions() -> None:
    bounds = CapacityBounds(lanes_floor=2, lanes_ceiling=5)
    assert bounds.clamp_lanes(1) == 2
    assert bounds.clamp_lanes(9) == 5
    assert bounds.clamp_lanes(3) == 3


# ---------------------------------------------------------------------- loop


def test_tick_publishes_a_capacity_file_and_an_event(tmp_path: Path) -> None:
    from agent_fleet.serve.events import read_serve_events
    from agent_fleet.serve.paths import capacity_path, items_path
    from agent_fleet.serve.serve import ServeLoop

    cgroup = tmp_path / "cg"
    (cgroup / "agents.slice").mkdir(parents=True)
    (cgroup / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (cgroup / "agents.slice" / "cpu.pressure").write_text(
        "some avg10=1.00 avg60=1.00 avg300=1.00 total=10\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=5\n",
        encoding="utf-8",
    )
    (cgroup / "agents.slice" / "memory.current").write_text("100", encoding="utf-8")
    (cgroup / "agents.slice" / "memory.max").write_text("1000", encoding="utf-8")

    loop = ServeLoop(
        operator="op",
        config=ServeConfig(operator="op", tick_seconds=0.01),
        clock=FakeClock(),
        cgroup_root=cgroup,
        max_ticks=2,
    )
    result = loop.tick()
    assert result.capacity_published is True
    assert result.degraded is False
    assert capacity_path("op").exists()
    assert items_path("op").parent.is_dir()
    events = [e["event"] for e in read_serve_events("op")]
    assert "serve.tick" in events
    assert "serve.started" not in events, "tick() alone must not claim the supervisor started"


def test_tick_on_a_broken_cgroup_goes_degraded(tmp_path: Path) -> None:
    from agent_fleet.serve.capacity import read_capacity
    from agent_fleet.serve.paths import capacity_path
    from agent_fleet.serve.serve import ServeLoop

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    loop = ServeLoop(
        operator="op",
        config=ServeConfig(operator="op", cgroup="absent.slice", tick_seconds=0.01),
        clock=FakeClock(),
        cgroup_root=empty,
        max_ticks=1,
    )
    result = loop.tick()
    assert result.degraded is True
    document = read_capacity(capacity_path("op"))
    assert document is not None
    assert document["targets"]["degraded"] is True
    assert document["targets"]["max_lanes"] == 1
    assert document["pressure"]["ok"] is False


def test_run_refuses_a_second_supervisor_for_one_operator() -> None:
    """The double-dispatch failure mode, at the process level."""
    from agent_fleet.serve.paths import lock_path
    from agent_fleet.serve.serve import ServeLoop

    loop = ServeLoop(
        operator="op", config=ServeConfig(operator="op", tick_seconds=0.01), max_ticks=1
    )
    import fcntl

    handle = lock_path("op").open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert loop.run() == 3, "a second supervisor must exit, not race"
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_run_stops_after_max_ticks() -> None:
    from agent_fleet.serve.serve import ServeLoop

    loop = ServeLoop(
        operator="op", config=ServeConfig(operator="op", tick_seconds=0.01), max_ticks=3
    )
    assert loop.run() == 0
    assert loop._tick_index == 3
