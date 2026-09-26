"""Capacity control — including every way the pressure source can fail.

The dangerous property of a capacity controller is not a wrong AIMD curve; it
is failing *open*. If the pressure source is unreadable and the controller
treats that as zero pressure, it ratchets lanes to the ceiling on a machine
that is saturated — and on this box ``/sys/fs/cgroup/agents.slice`` genuinely
does not exist, because systemd nests the user manager's slice. So the
missing-source cases get as much attention as the happy path.

The load-average ban is asserted structurally, over the AST, rather than by
grepping for a string: ``from os import getloadavg`` and a literal
``/proc/loadavg`` read both slip past a text search.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from agent_fleet.serve.capacity import (
    CAPACITY_SCHEMA,
    SIGNAL_EASE,
    SIGNAL_HOLD,
    SIGNAL_PRESSURE,
    SIGNAL_UNKNOWN,
    CapacityBounds,
    CapacityController,
    CapacityPolicy,
    Watermarks,
    capacity_document,
    classify,
    read_capacity,
    write_capacity,
)
from agent_fleet.serve.clock import FakeClock
from agent_fleet.serve.pressure import (
    CpuPressure,
    PressureReading,
    detect_hierarchy,
    read_pressure,
    resolve_cgroup,
)

SERVE_DIR = Path(__file__).resolve().parent.parent / "agent_fleet" / "serve"


# --------------------------------------------------------------------- fixtures


def _v2_tree(
    root: Path,
    name: str = "agents.slice",
    *,
    some_avg60: float = 0.0,
    total_us: int = 12345,
    memory_used: int = 1_000_000,
    memory_max: int = 10_000_000,
) -> Path:
    """A cgroup v2 tree the controller can actually read."""
    cg = root / name
    cg.mkdir(parents=True, exist_ok=True)
    (root / "cgroup.controllers").write_text("cpu memory io\n", encoding="utf-8")
    (cg / "cpu.pressure").write_text(
        f"some avg10={some_avg60 / 6:.2f} avg60={some_avg60:.2f} avg300={some_avg60:.2f} "
        f"total={total_us}\n"
        f"full avg10=0.00 avg60=0.00 avg300=0.00 total={total_us // 2}\n",
        encoding="utf-8",
    )
    (cg / "memory.current").write_text(str(memory_used), encoding="utf-8")
    (cg / "memory.max").write_text(str(memory_max), encoding="utf-8")
    return cg


def _reading(
    *,
    ok: bool = True,
    avg60: float = 0.0,
    memory_ratio: float | None = 0.1,
    error: str = "",
) -> PressureReading:
    return PressureReading(
        ok=ok,
        error=error,
        path="/fake/cgroup" if ok else "",
        cpu=CpuPressure(some_avg60=avg60, some_total_us=999),
        memory_used_bytes=1 if memory_ratio is not None else None,
        memory_max_bytes=10 if memory_ratio is not None else None,
        memory_ratio=memory_ratio,
    )


def _policy(**kwargs: object) -> CapacityPolicy:
    bounds = CapacityBounds(
        lanes_floor=1,
        lanes_ceiling=10,
        gates_floor=1,
        gates_ceiling=6,
        tests_floor=1,
        tests_ceiling=4,
        typecheck_floor=1,
        typecheck_ceiling=4,
    )
    defaults: dict[str, object] = {
        "bounds": bounds,
        "marks": Watermarks(low=10.0, high=25.0, memory_low=0.7, memory_high=0.85),
        "step": 1,
        "decrease": 0.5,
        "starvation_ticks": 3,
    }
    defaults.update(kwargs)
    return CapacityPolicy(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------ no loadavg


def test_serve_never_references_the_load_average() -> None:
    """Structurally, not by text search.

    Under a cgroup CPU quota the run queue counts throttled tasks, not work
    waiting for a CPU: a 4-way quota on a 16-core box reports a load of 64 with
    every core idle. Admitting more work in response makes it worse, which is
    why the controller reads PSI instead.
    """
    offenders: list[str] = []
    for path in sorted(SERVE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("getloadavg", "loadavg"):
                offenders.append(f"{path.name}:{node.lineno} attribute {node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    if alias.name in ("getloadavg",):
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "/proc/loadavg" in node.value
            ):
                offenders.append(f"{path.name}:{node.lineno} reads /proc/loadavg")
    assert not offenders, f"serve must not use the load average: {offenders}"


# ------------------------------------------------------------------- pressure


def test_reads_a_v2_cgroup(tmp_path: Path) -> None:
    _v2_tree(tmp_path, some_avg60=12.5, memory_used=8_000_000, memory_max=10_000_000)
    reading = read_pressure("agents.slice", root=tmp_path)
    assert reading.ok is True
    assert reading.hierarchy == "v2"
    assert reading.cpu.some_avg60 == pytest.approx(12.5)
    assert reading.memory_ratio == pytest.approx(0.8)


def test_a_missing_cgroup_is_a_failed_read_not_an_idle_machine(tmp_path: Path) -> None:
    """The fail-open bug this pins.

    A reader that turned "file not found" into 0.0 would report no pressure,
    and the controller would ramp to its ceiling on a saturated machine.
    """
    (tmp_path / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    reading = read_pressure("nope.slice", root=tmp_path)
    assert reading.ok is False
    assert reading.source_ok is False
    assert "not found" in reading.error
    assert reading.path == ""


def test_missing_cpu_pressure_file_is_a_failed_read(tmp_path: Path) -> None:
    cg = tmp_path / "agents.slice"
    cg.mkdir(parents=True)
    (tmp_path / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (cg / "memory.current").write_text("1", encoding="utf-8")
    reading = read_pressure("agents.slice", root=tmp_path)
    assert reading.ok is False
    assert "cpu.pressure" in reading.error


def test_memory_max_of_max_means_unlimited_not_a_huge_number(tmp_path: Path) -> None:
    """``max`` is the string unlimited. Parsing it as an int would make the
    memory ratio a meaningless tiny fraction and stop memory ever registering."""
    _v2_tree(tmp_path)
    (tmp_path / "agents.slice" / "memory.max").write_text("max", encoding="utf-8")
    reading = read_pressure("agents.slice", root=tmp_path)
    assert reading.ok is True
    assert reading.memory_max_bytes is None
    assert reading.memory_ratio is None


def test_v1_hierarchy_reports_unavailable_and_still_gives_memory(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir(parents=True)
    (tmp_path / "cpu,cpuacct").mkdir(parents=True)
    cg = tmp_path / "agents.slice"
    cg.mkdir()
    (cg / "memory.usage_in_bytes").write_text("5000", encoding="utf-8")
    (cg / "memory.limit_in_bytes").write_text("10000", encoding="utf-8")
    assert detect_hierarchy(tmp_path) == "v1"
    reading = read_pressure("agents.slice", root=tmp_path)
    assert reading.ok is False
    assert reading.hierarchy == "v1"
    assert reading.memory_ratio == pytest.approx(0.5)
    assert "v1" in reading.error


def test_v1_unlimited_sentinel_is_treated_as_unlimited(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir(parents=True)
    cg = tmp_path / "agents.slice"
    cg.mkdir()
    (cg / "memory.usage_in_bytes").write_text("5000", encoding="utf-8")
    (cg / "memory.limit_in_bytes").write_text("9223372036854771712", encoding="utf-8")
    reading = read_pressure("agents.slice", root=tmp_path)
    assert reading.memory_max_bytes is None
    assert reading.memory_ratio is None


def test_a_real_idle_cgroup_reads_as_ok_with_zero_pressure(tmp_path: Path) -> None:
    """Zero pressure from a *readable* file is a healthy reading.

    The distinction from a failed read is the whole design: here `ok` is True
    and the controller is entitled to increase capacity.
    """
    _v2_tree(tmp_path, some_avg60=0.0, total_us=0)
    reading = read_pressure("agents.slice", root=tmp_path)
    assert reading.ok is True
    assert reading.cpu.some_avg60 == 0.0


def test_resolve_cgroup_finds_a_bare_name_under_systemd_nesting(tmp_path: Path) -> None:
    """`agents.slice` does not exist at the cgroup root on a systemd box.

    The real path is user.slice/user-N.slice/user@N.service/agents.slice, so a
    bare name is searched under a bounded set of nesting prefixes rather than
    assumed to be /sys/fs/cgroup/<name>.
    """
    nested = tmp_path / "user.slice" / "user-1000.slice" / "user@1000.service"
    (nested / "agents.slice").mkdir(parents=True)
    found = resolve_cgroup("agents.slice", root=tmp_path)
    assert found is not None
    assert found.name == "agents.slice"
    assert "user@1000.service" in str(found)


def test_resolve_cgroup_returns_none_for_an_unknown_name(tmp_path: Path) -> None:
    (tmp_path / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    assert resolve_cgroup("absent.slice", root=tmp_path) is None


def test_resolve_cgroup_accepts_an_explicit_subpath(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert resolve_cgroup("a/b", root=tmp_path) == tmp_path / "a" / "b"


# --------------------------------------------------------------------- classify


def test_classify_reports_ease_below_the_low_watermark() -> None:
    signal, reason = classify(_reading(avg60=1.0, memory_ratio=0.1), _policy().marks)
    assert signal == SIGNAL_EASE
    assert "cpu" in reason


def test_classify_holds_inside_the_hysteresis_band() -> None:
    """Between the watermarks nothing moves.

    Without this band a controller oscillating across one threshold burns half
    its decisions flipping and settles on neither the old nor the new capacity.
    """
    signal, _ = classify(_reading(avg60=15.0, memory_ratio=0.1), _policy().marks)
    assert signal == SIGNAL_HOLD


def test_classify_reports_pressure_above_the_high_watermark() -> None:
    signal, _ = classify(_reading(avg60=40.0, memory_ratio=0.1), _policy().marks)
    assert signal == SIGNAL_PRESSURE


def test_classify_prefers_memory_when_memory_is_the_severe_signal() -> None:
    """Memory pressure outranks CPU stall: running out kills work, stalling slows it."""
    signal, reason = classify(_reading(avg60=1.0, memory_ratio=0.95), _policy().marks)
    assert signal == SIGNAL_PRESSURE
    assert "memory" in reason


def test_classify_reports_unknown_for_a_failed_read() -> None:
    signal, reason = classify(_reading(ok=False, error="cgroup gone"), _policy().marks)
    assert signal == SIGNAL_UNKNOWN
    assert "cgroup gone" in reason


# ----------------------------------------------------------------- AIMD itself


def test_additive_increase_under_low_pressure() -> None:
    controller = CapacityController(_policy(), clock=FakeClock())
    controller.tick(_reading(avg60=0.0))
    first = controller.targets.max_lanes
    controller.tick(_reading(avg60=0.0))
    assert controller.targets.max_lanes == first + 1, "additive increase, not multiplicative"


def test_multiplicative_decrease_under_high_pressure() -> None:
    """Ramp first, then measure one decrease.

    Completion is fed in during the ramp so the starvation guard stays out of
    it — with an empty queue and no completions the controller is *supposed* to
    collapse lanes to the floor instead, which is a different (separately
    tested) rule.
    """
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(4):
        controller.tick(_reading(avg60=0.0), completed=1)
    high = controller.targets.max_lanes
    assert high == 5
    targets = controller.tick(_reading(avg60=99.0), completed=1)
    assert targets.max_lanes == int(high * 0.5)
    assert targets.max_gates == int(5 * 0.5)


def test_targets_never_exceed_the_ceiling() -> None:
    """The ceiling is a hard limit, not advice.

    Applying it after the adjustment rather than by clamping the input is what
    makes it hold on the one tick where pressure spikes.
    """
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(50):
        controller.tick(_reading(avg60=0.0), completed=1)
    assert controller.targets.max_lanes == 10
    assert controller.targets.max_gates == 6


def test_targets_never_fall_below_the_floor() -> None:
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(20):
        controller.tick(_reading(avg60=99.0), completed=1)
    assert controller.targets.max_lanes == 1
    assert controller.targets.max_gates == 1
    assert controller.targets.test_pool == 1
    assert controller.targets.typecheck_pool == 1


def test_unreadable_pressure_falls_to_the_floor_and_marks_degraded() -> None:
    """The fail-closed direction, which is the opposite of the healthy one."""
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(5):
        controller.tick(_reading(avg60=0.0), completed=1)
    assert controller.targets.max_lanes == 6  # floor 1, five additive increases
    targets = controller.tick(_reading(ok=False, error="no such cgroup"))
    assert targets.max_lanes == 1
    assert targets.max_gates == 1
    assert targets.degraded is True
    assert targets.signal == SIGNAL_UNKNOWN
    assert "no such cgroup" in targets.reason
    assert controller.degraded_ticks == 1


def test_degraded_ticks_reset_when_the_source_recovers() -> None:
    controller = CapacityController(_policy(), clock=FakeClock())
    controller.tick(_reading(ok=False, error="down"))
    controller.tick(_reading(ok=False, error="down"))
    assert controller.degraded_ticks == 2
    controller.tick(_reading(avg60=0.0))
    assert controller.degraded_ticks == 0
    assert controller.targets.degraded is False


def test_a_failed_read_never_increases_capacity() -> None:
    """One property, many ticks: a broken source cannot ratchet anything up."""
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(30):
        controller.tick(_reading(ok=False, error="missing"))
    assert controller.targets.max_lanes == 1


# ------------------------------------------------------------------- starvation


def test_starvation_guard_collapses_lanes_and_prioritises_gates() -> None:
    """Saturated with nothing completing: gates unblock the queue, more lanes cannot."""
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(3):
        controller.tick(_reading(avg60=0.0))
    starved = controller.tick(_reading(avg60=99.0))
    assert starved.gates_priority is True
    assert starved.max_lanes == 1
    assert "starved" in starved.reason


def test_starvation_guard_needs_saturation_too() -> None:
    """Idle ticks under *no* pressure are a quiet fleet, not a stuck one."""
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(6):
        targets = controller.tick(_reading(avg60=0.0))
    assert targets.gates_priority is False


def test_a_completion_resets_the_starvation_counter() -> None:
    controller = CapacityController(_policy(), clock=FakeClock())
    controller.tick(_reading(avg60=99.0))
    controller.tick(_reading(avg60=99.0))
    assert controller.idle_ticks == 2
    controller.tick(_reading(avg60=99.0), completed=1)
    assert controller.idle_ticks == 0


def test_starvation_clears_once_throughput_resumes() -> None:
    controller = CapacityController(_policy(), clock=FakeClock())
    for _ in range(4):
        controller.tick(_reading(avg60=99.0))
    assert controller.targets.gates_priority is True
    controller.tick(_reading(avg60=99.0), completed=2)
    assert controller.targets.gates_priority is False


# --------------------------------------------------------------- state carry-over


def test_restore_resumes_the_ramp_instead_of_restarting_at_floor() -> None:
    """Without this, every serve restart would collapse capacity to the floor
    and spend minutes climbing back, so the fleet looks like it just recovered
    from an incident each time the supervisor bounces."""
    first = CapacityController(_policy(), clock=FakeClock())
    for _ in range(4):
        first.tick(_reading(avg60=0.0))
    document = capacity_document("op", first.targets, _reading(), clock=FakeClock())
    second = CapacityController(_policy(), clock=FakeClock())
    second.restore(document)
    assert second.targets.max_lanes == first.targets.max_lanes
    assert second.tick(_reading(avg60=0.0)).max_lanes == first.targets.max_lanes + 1


def test_restore_tolerates_a_partial_or_missing_payload() -> None:
    controller = CapacityController(_policy(), clock=FakeClock())
    controller.restore({})
    controller.restore({"targets": None, "idle_ticks": "x", "saturated": "y"})
    assert controller.idle_ticks == 0
    assert controller.saturated is False


# --------------------------------------------------------------- the file itself


def test_capacity_file_has_the_documented_schema(tmp_path: Path) -> None:
    controller = CapacityController(_policy(), clock=FakeClock())
    targets = controller.tick(_reading(avg60=5.0))
    path = write_capacity(
        "op", targets, _reading(avg60=5.0), clock=FakeClock(), path=tmp_path / "c.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == CAPACITY_SCHEMA
    assert payload["operator"] == "op"
    assert set(payload) == {
        "schema",
        "operator",
        "updated_epoch",
        "targets",
        "pressure",
        "idle_ticks",
        "saturated",
    }
    assert set(payload["targets"]) == {
        "max_lanes",
        "max_gates",
        "test_pool",
        "typecheck_pool",
        "signal",
        "reason",
        "gates_priority",
        "degraded",
    }
    assert "ok" in payload["pressure"]


def test_capacity_file_preserves_the_failure_pressure_ok_false(tmp_path: Path) -> None:
    """A reader must be able to tell "no pressure" from "no reading"."""
    controller = CapacityController(_policy(), clock=FakeClock())
    targets = controller.tick(_reading(ok=False, error="gone"))
    path = write_capacity(
        "op", targets, _reading(ok=False, error="gone"), clock=FakeClock(), path=tmp_path / "c.json"
    )
    payload = read_capacity(path)
    assert payload is not None
    assert payload["pressure"]["ok"] is False
    assert payload["targets"]["degraded"] is True
    assert payload["targets"]["max_lanes"] == 1


def test_read_capacity_returns_none_for_missing_or_corrupt(tmp_path: Path) -> None:
    assert read_capacity(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert read_capacity(bad) is None
    notdict = tmp_path / "list.json"
    notdict.write_text("[1,2]", encoding="utf-8")
    assert read_capacity(notdict) is None


def test_every_watermark_ordering_is_validated() -> None:
    with pytest.raises(ValueError, match="low watermark"):
        Watermarks(low=30.0, high=10.0)
    with pytest.raises(ValueError, match="memory low watermark"):
        Watermarks(memory_low=0.9, memory_high=0.8)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's capacity file inside its own tmp_path."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
