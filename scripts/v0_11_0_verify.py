"""v0.11.0 verification harness.

Measures the four done-predicate dimensions against the live code so each unit
lands as a measured old-versus-new delta. Re-run before and after every unit.

  D1 control surface   the program-runtime primitive set, and whether the three
                       new bounded primitives (replan/branch/subprogram) exist.
  D2 elastic bound     how many hand-copied effective_capacity(reserved=1) sites
                       remain (Unit 3 drives this to zero); a gate-routed load
                       test proving a wide nested rank QUEUES against the RAM
                       ceiling instead of denying, even with an oversized worker
                       pool; a raw try_admit probe as the denial-visibility floor;
                       whether the blocking AdmissionGate acquire path exists.
  D3 crash-resume      whether a run journal, a fold, and a resume entry point
                       exist.
  D4 queryable log     whether a query/fold API over run events exists.

The exit code reflects harness integrity, not product readiness: it is nonzero
only if a measurement throws or the raw admission probe fails to reproduce
denials (which would mean the harness cannot see the thing it measures).
"""

from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MethodType
from typing import TYPE_CHECKING, cast
from unittest import mock

from agent_fleet.admission import AdmissionController, AdmissionDenied, ResourceTier
from agent_fleet.config import FleetConfig
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.dag import runner as _runner
from agent_fleet.orchestration.dag.runner import dispatch_dag
from agent_fleet.orchestration.dag.schema import DagSpec, DagTask
from agent_fleet.orchestration.program.runtime import ProgramContext

if TYPE_CHECKING:
    from agent_fleet.orchestration.types import _DispatcherLike

_DENIED = "Fleet admission denied (max parallel agents reached)"
_PKG_ROOT = Path(__file__).resolve().parent.parent / "agent_fleet"
_NEW_PRIMITIVES = ("replan", "branch", "subprogram")


@dataclass(frozen=True)
class SurfaceResult:
    primitives: list[str]
    new_present: list[str]
    new_absent: list[str]

    @property
    def width(self) -> int:
        return len(self.primitives)


@dataclass(frozen=True)
class ReservedResult:
    sites: list[str]

    @property
    def count(self) -> int:
        return len(self.sites)


@dataclass(frozen=True)
class ProbeResult:
    found: list[str]

    @property
    def present(self) -> bool:
        return bool(self.found)


@dataclass(frozen=True)
class LoadResult:
    completed: int
    width: int
    denied: int
    peak: int
    cap: int
    status: str
    label: str


def measure_control_surface() -> SurfaceResult:
    ctx = ProgramContext(
        dispatcher=cast("_DispatcherLike", object()),
        persona_resolver=None,
        default_persona="coder",
        default_pipeline="simple",
        max_parallel=4,
        max_agents=10,
        timeout_s=None,
        fleet_log=None,
    )
    namespace = ctx.build_namespace()
    primitives = sorted(k for k in namespace if k != "__builtins__")
    return SurfaceResult(
        primitives=primitives,
        new_present=[n for n in _NEW_PRIMITIVES if n in namespace],
        new_absent=[n for n in _NEW_PRIMITIVES if n not in namespace],
    )


def count_reserved_sites() -> ReservedResult:
    sites: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "reserved=1" in line:
                sites.append(f"{path.relative_to(_PKG_ROOT.parent)}:{lineno}")
    return ReservedResult(sites=sites)


def probe(targets: list[tuple[str, str]]) -> ProbeResult:
    found: list[str] = []
    for module_path, attr in targets:
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            continue
        if not attr or hasattr(module, attr):
            found.append(f"{module_path}.{attr}" if attr else module_path)
    return ProbeResult(found=found)


class _Resolver:
    def list_personas(self) -> list[str]:
        return ["coder"]

    def load(self, name, *, loadout_size=None):  # noqa: ANN001, ANN202
        raise NotImplementedError


def _wide_dag(width: int) -> DagSpec:
    return DagSpec(
        title="wide-rank",
        tasks=tuple(
            DagTask(id=f"t{i}", depends_on=(), complexity="LOW", subtask_prompt=f"task {i}")
            for i in range(width)
        ),
    )


def _run_gated_dag(disp, width: int, cap: int, *, label: str) -> LoadResult:  # noqa: ANN001
    """Drive a wide rank through dispatch_dag with a gate-routed fake executor.

    Overflow must QUEUE on the AdmissionGate, never deny: the gate denies only
    when a caller's ancestors hold every slot (depth >= cap), which a single
    token-holding parent at depth 1 never triggers for cap >= 2.
    """
    live = 0
    peak = 0
    denied = 0
    metrics = threading.Lock()

    def fake_execute(self, task_index, task, *, depth=0, **_):  # noqa: ANN001, ANN003, ANN202
        nonlocal live, peak, denied
        try:
            token = self._gate.acquire_token(depth=depth)
        except AdmissionDenied:
            with metrics:
                denied += 1
            return FleetTaskResult(
                task_index=task_index, persona=task.persona, goal=task.goal,
                status="error", summary=None, error=_DENIED, duration_seconds=0.0,
            )
        try:
            with metrics:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)
            return FleetTaskResult(
                task_index=task_index, persona=task.persona, goal=task.goal,
                status="completed", summary="ok", error=None, duration_seconds=0.02,
            )
        finally:
            self._gate.release(token)
            with metrics:
                live -= 1

    disp._execute_task = MethodType(fake_execute, disp)
    parent = FleetTask(goal="parent", persona="coder", pipeline="simple")
    ptoken = disp._gate.acquire_token(depth=0)
    try:
        summary = dispatch_dag(
            spec=_wide_dag(width),
            parent_task=parent,
            dispatcher=disp,
            persona_resolver=_Resolver(),
            fallback_persona="coder",
            default_pipeline="simple",
        )
    finally:
        disp._gate.release(ptoken)

    completed = sum(1 for r in summary.results if r.status == "completed")
    return LoadResult(
        completed=completed,
        width=width,
        denied=denied,
        peak=peak,
        cap=cap,
        status=summary.aggregate_status,
        label=label,
    )


def _raw_admission_probe(*, cap: int = 3, width: int = 8) -> int:
    """Denial-visibility floor: try_admit past capacity must return None.

    This is the integrity check the gate-routed load test cannot be: the gate
    queues overflow, so it never denies at depth 1. The raw controller still
    refuses once full, proving the harness can observe a denial at all.
    """
    controller = AdmissionController(
        ram_budget_gb=cap * 4,
        tiers={"agent": ResourceTier("agent", ram_gb=4, max_concurrent=cap)},
    )
    held = []
    denied = 0
    for _ in range(width):
        token = controller.try_admit("agent")
        if token is None:
            denied += 1
        else:
            held.append(token)
    for token in held:
        controller.release(token)
    return denied


def measure_load() -> tuple[LoadResult, LoadResult, int]:
    tight = FleetDispatcher(config=FleetConfig(max_parallel=3, ram_budget_gb=24))
    tcap = min(int(tight.config.max_parallel), int(tight.config.ram_budget_gb) // 4)
    bounded = _run_gated_dag(tight, width=8, cap=tcap, label="bounded pool")

    big = FleetDispatcher(config=FleetConfig(max_parallel=3, ram_budget_gb=24))
    with mock.patch.object(_runner, "effective_capacity", lambda *_a, **_k: 8):
        oversized = _run_gated_dag(big, width=8, cap=tcap, label="oversized pool")

    raw_denied = _raw_admission_probe(cap=3, width=8)
    return bounded, oversized, raw_denied


def main() -> int:
    print("=== agent-fleet v0.11.0 verification harness ===\n")

    surface = measure_control_surface()
    print("[D1] control surface")
    print(f"  primitives ({surface.width}): {', '.join(surface.primitives)}")
    print(f"  new present: {surface.new_present or 'none'}")
    print(f"  new absent : {surface.new_absent or 'none'}\n")

    reserved = count_reserved_sites()
    print("[D2] elastic bound")
    print(f"  hand-copied reserved=1 sites: {reserved.count} (Unit 3 target: 0)")
    for site in reserved.sites:
        print(f"    {site}")
    gate = probe([("agent_fleet.admission", "AdmissionGate")])
    print(f"  blocking AdmissionGate present: {gate.present} ({gate.found or 'absent'})")

    bounded, oversized, raw_denied = measure_load()
    for load in (bounded, oversized):
        print(
            f"  load test ({load.label}, cap={load.cap}, width={load.width}): "
            f"completed={load.completed}/{load.width} "
            f"denied={load.denied} peak={load.peak} status={load.status}"
        )
    print(f"  raw try_admit probe (denial-visibility floor): denied={raw_denied}\n")

    resume = probe([
        ("agent_fleet.orchestration.resume", "resume_run"),
        ("agent_fleet.orchestration.journal", "resume_run"),
    ])
    journal_fold = probe([
        ("agent_fleet.orchestration.journal", "fold"),
        ("agent_fleet.orchestration.journal", "RunState"),
    ])
    print("[D3] crash-resume")
    print(f"  resume entry point present: {resume.present} ({resume.found or 'absent'})\n")
    print("[D4] queryable event log")
    found_label = journal_fold.found or "absent"
    print(f"  journal fold/query present: {journal_fold.present} ({found_label})\n")

    gate_clean = bounded.denied == 0 and oversized.denied == 0
    if not gate_clean:
        print("HARNESS INTEGRITY: FAIL (gate denied under a single-parent fan-out; should queue)")
        return 1
    if raw_denied < 1:
        print("HARNESS INTEGRITY: FAIL (raw probe saw no denial; cannot observe the bound)")
        return 1
    print(
        "HARNESS INTEGRITY: OK "
        "(gate queued overflow with zero denials; raw probe confirms denials are observable)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
