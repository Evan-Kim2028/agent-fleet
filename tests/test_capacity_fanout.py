"""Regression tests for capacity-aware orchestration fanout.

v0.11.0 (Unit 3) replaced the hand-copied ``reserved=1`` reservation with a
blocking ``AdmissionGate``: a wide fan-out queues against the RAM ceiling rather
than being denied, and the gate denies only the structural-deadlock case where a
caller's ancestors already hold every slot. These tests pin that contract.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from agent_fleet.admission import (
    AdmissionController,
    AdmissionDenied,
    AdmissionGate,
    ResourceTier,
    Token,
)
from agent_fleet.config import FleetConfig
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration import primitives
from agent_fleet.orchestration.dag.runner import dispatch_dag
from agent_fleet.orchestration.dag.schema import DagSpec, DagTask
from agent_fleet.orchestration.primitives import effective_capacity
from agent_fleet.orchestration.program import run_workflow_program

if TYPE_CHECKING:
    from agent_fleet.hooks import PersonaResolver

_ADMISSION_DENIED = "Fleet admission denied (max parallel agents reached)"
_FLAKES = 30


class _StubPersonaResolver:
    def list_personas(self) -> list[str]:
        return ["coder"]

    def load(self, name: str, *, loadout_size: str | None = None) -> object:
        raise NotImplementedError


@dataclass
class _AdmissionDispatcher:
    """Stand-in whose ``_execute_task`` admits through a real ``AdmissionGate``."""

    config: FleetConfig
    _admission: AdmissionController = field(init=False)
    _gate: AdmissionGate = field(init=False)
    _metrics_lock: threading.Lock = field(default_factory=threading.Lock)
    peak_children: int = 0
    denied_count: int = 0
    _current_children: int = 0
    work_sleep_s: float = 0.02

    def __post_init__(self) -> None:
        self._admission = AdmissionController(
            ram_budget_gb=self.config.ram_budget_gb,
            tiers={
                "agent": ResourceTier(
                    "agent",
                    ram_gb=4,
                    max_concurrent=self.config.max_parallel,
                ),
            },
        )
        self._gate = AdmissionGate(self._admission, tier="agent")

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        depth: int = 0,
        **_: object,
    ) -> FleetTaskResult:
        try:
            token = self._gate.acquire_token(depth=depth)
        except AdmissionDenied:
            with self._metrics_lock:
                self.denied_count += 1
            return FleetTaskResult(
                task_index=task_index,
                persona=task.persona,
                goal=task.goal,
                status="error",
                summary=None,
                error=_ADMISSION_DENIED,
                duration_seconds=0.0,
            )
        try:
            with self._metrics_lock:
                self._current_children += 1
                self.peak_children = max(self.peak_children, self._current_children)
            time.sleep(self.work_sleep_s)
            node_id = (task.title or task.goal).rsplit(" — ", 1)[-1]
            return FleetTaskResult(
                task_index=task_index,
                persona=task.persona,
                goal=task.goal,
                status="completed",
                summary=f"done {node_id}",
                error=None,
                duration_seconds=self.work_sleep_s,
            )
        finally:
            self._gate.release(token)
            with self._metrics_lock:
                self._current_children -= 1


def _config(*, max_parallel: int, ram_budget_gb: int = 24) -> FleetConfig:
    return FleetConfig(max_parallel=max_parallel, ram_budget_gb=ram_budget_gb)


def _hold_parent_token(dispatcher: _AdmissionDispatcher) -> Token:
    return dispatcher._gate.acquire_token(depth=0)


def _release_parent_token(dispatcher: _AdmissionDispatcher, token: Token) -> None:
    dispatcher._gate.release(token)


def _wide_rank_spec(width: int = 8) -> DagSpec:
    tasks = tuple(
        DagTask(
            id=f"t{i}",
            depends_on=(),
            complexity="LOW",
            subtask_prompt=f"task {i}",
        )
        for i in range(width)
    )
    return DagSpec(title="wide-rank", tasks=tasks)


def _count_denied(results: list[FleetTaskResult]) -> int:
    return sum(1 for r in results if r.error == _ADMISSION_DENIED)


# ---------------------------------------------------------------------------
# effective_capacity (now just a worker-count hint; no reservation arithmetic)
# ---------------------------------------------------------------------------


def test_effective_capacity_is_admission_capacity() -> None:
    d = _AdmissionDispatcher(config=_config(max_parallel=3))
    assert effective_capacity(d, fallback=99) == 3


def test_effective_capacity_ram_bound() -> None:
    d = _AdmissionDispatcher(config=_config(max_parallel=10, ram_budget_gb=24))
    assert effective_capacity(d, fallback=99) == 6


def test_effective_capacity_no_config_uses_fallback() -> None:
    assert effective_capacity(object(), fallback=7) == 7


def test_ram_mirror_drift_guard() -> None:
    dispatcher = FleetDispatcher()
    assert dispatcher._admission._tiers["agent"].ram_gb == primitives._RAM_GB_PER_AGENT


# ---------------------------------------------------------------------------
# AdmissionGate contract: queue overflow, deny only the deadlock case
# ---------------------------------------------------------------------------


def test_gate_queues_overflow_to_capacity_minus_ancestors() -> None:
    """Elastic bound: a held parent token (depth 0) plus 8 children at depth 1
    under cap=3 admits at most cap-1=2 children at once and queues the rest —
    none denied, all complete. The parent's slot is reserved by the shared
    controller, not by reservation arithmetic."""
    controller = AdmissionController(
        ram_budget_gb=24,
        tiers={"agent": ResourceTier("agent", ram_gb=4, max_concurrent=3)},
    )
    gate = AdmissionGate(controller, tier="agent")
    parent = gate.acquire_token(depth=0)
    live = 0
    peak = 0
    lock = threading.Lock()

    def child() -> None:
        nonlocal live, peak
        token = gate.acquire_token(depth=1)
        try:
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)
        finally:
            with lock:
                live -= 1
            gate.release(token)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(child) for _ in range(8)]
            for fut in as_completed(futures):
                fut.result()
    finally:
        gate.release(parent)
    assert peak == 2, peak


def test_gate_denies_when_ancestors_hold_every_slot() -> None:
    """The single deny case: at depth >= capacity every slot is held by an
    ancestor that will not release until this subtree finishes, so blocking
    would deadlock. The gate refuses, preserving liveness — and keeping denials
    observable so the zero-denial assertions above remain meaningful."""
    controller = AdmissionController(
        ram_budget_gb=24,
        tiers={"agent": ResourceTier("agent", ram_gb=4, max_concurrent=3)},
    )
    gate = AdmissionGate(controller, tier="agent")
    cap = controller.capacity_for("agent")
    raised = False
    try:
        gate.acquire_token(depth=cap)
    except AdmissionDenied:
        raised = True
    assert raised, "depth >= capacity must deny, not block"


# ---------------------------------------------------------------------------
# Nested DAG wide rank
# ---------------------------------------------------------------------------


def _run_nested_wide_dag_once() -> tuple[int, int, str, int]:
    dispatcher = _AdmissionDispatcher(config=_config(max_parallel=3))
    parent = FleetTask(goal="parent", persona="coder", pipeline="simple")
    token = _hold_parent_token(dispatcher)
    try:
        summary = dispatch_dag(
            spec=_wide_rank_spec(8),
            parent_task=parent,
            dispatcher=cast("FleetDispatcher", dispatcher),
            persona_resolver=cast("PersonaResolver", _StubPersonaResolver()),
            fallback_persona="coder",
            default_pipeline="simple",
        )
    finally:
        _release_parent_token(dispatcher, token)
    denied = _count_denied(summary.results)
    return (
        sum(1 for r in summary.results if r.status == "completed"),
        denied,
        summary.aggregate_status,
        dispatcher.peak_children,
    )


def test_nested_dag_wide_rank_queues_without_denials() -> None:
    for _ in range(_FLAKES):
        completed, denied, status, peak = _run_nested_wide_dag_once()
        assert completed == 8, (completed, denied, status, peak)
        assert denied == 0, (completed, denied, status, peak)
        assert status == "completed", (completed, denied, status, peak)
        assert peak <= 2, (completed, denied, status, peak)


# ---------------------------------------------------------------------------
# Nested runtime fanout
# ---------------------------------------------------------------------------

_PARALLEL_EIGHT = """
thunks = [lambda i=i: agent(f'task {i}') for i in range(8)]
results = parallel(thunks)
return len(results)
"""


def _run_nested_program_once(*, max_parallel: int) -> tuple[int, int]:
    dispatcher = _AdmissionDispatcher(config=_config(max_parallel=max_parallel))
    token = _hold_parent_token(dispatcher)
    try:
        run_workflow_program(
            _PARALLEL_EIGHT,
            dispatcher=dispatcher,
            default_persona="coder",
            max_parallel=16,
        )
    finally:
        _release_parent_token(dispatcher, token)
    return dispatcher.denied_count, dispatcher.peak_children


def test_nested_runtime_fanout_cap3_zero_denials() -> None:
    for _ in range(_FLAKES):
        denied, peak = _run_nested_program_once(max_parallel=3)
        assert denied == 0, (denied, peak)
        assert peak <= 2, (denied, peak)


def test_nested_runtime_fanout_cap6_scales_peak() -> None:
    for _ in range(_FLAKES):
        denied, peak = _run_nested_program_once(max_parallel=6)
        assert denied == 0, (denied, peak)
        assert peak > 3, (denied, peak)
