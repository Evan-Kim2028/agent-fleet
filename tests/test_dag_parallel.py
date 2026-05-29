"""Proves dependency-driven scheduling: Fc starts before S finishes.

Old rank-barrier: Fc waited for the whole first rank (F and S) to complete.
Dependency-driven: Fc starts as soon as F finishes, independent of S.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import cast

from agent_fleet.config import load_fleet_config
from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.dag.runner import dispatch_dag
from agent_fleet.orchestration.dag.schema import DagSpec, DagTask
from agent_fleet.personas import YamlPersonaResolver

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent

_FAST_SLEEP = 0.02
_SLOW_SLEEP = 0.30


@dataclass
class _TimedDispatcher:
    """Records (node_id, monotonic start_time) and sleeps per node."""

    sleep_map: dict[str, float] = field(default_factory=dict)
    events: list[tuple[str, float]] = field(default_factory=list)  # (node_id, start_t)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        **_: object,
    ) -> FleetTaskResult:
        node_id = (task.title or "").rsplit(" — ", 1)[-1]
        t0 = time.monotonic()
        self.events.append((node_id, t0))
        time.sleep(self.sleep_map.get(node_id, 0.01))
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=f"done {node_id}",
            error=None,
            duration_seconds=self.sleep_map.get(node_id, 0.01),
        )


def _two_chain_spec() -> DagSpec:
    """Two independent chains sharing rank-0.

    F  -> Fc          (F is fast: ~FAST_SLEEP)
    S              (S is slow: ~SLOW_SLEEP, no dependents)
    """
    return DagSpec(
        title="two-chain",
        tasks=(
            DagTask(id="F", depends_on=(), complexity="LOW", subtask_prompt="fast root"),
            DagTask(id="S", depends_on=(), complexity="LOW", subtask_prompt="slow root"),
            DagTask(id="Fc", depends_on=("F",), complexity="LOW", subtask_prompt="fast child"),
        ),
    )


def test_fc_starts_before_s_finishes() -> None:
    """Fc must start before S finishes; under rank-barrier it could not."""
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    dispatcher = _TimedDispatcher(
        sleep_map={"F": _FAST_SLEEP, "S": _SLOW_SLEEP, "Fc": _FAST_SLEEP}
    )
    parent = FleetTask(goal="parent", persona="coder", pipeline="simple")

    summary = dispatch_dag(
        spec=_two_chain_spec(),
        parent_task=parent,
        dispatcher=cast("object", dispatcher),  # type: ignore[arg-type]
        persona_resolver=resolver,
        fallback_persona="coder",
        default_pipeline="simple",
    )

    assert summary.aggregate_status == "completed"

    by_id = dict(dispatcher.events)
    assert set(by_id.keys()) == {"F", "S", "Fc"}, f"Missing nodes in events: {by_id}"

    # S finishes at approximately:
    s_finish = by_id["S"] + _SLOW_SLEEP
    fc_start = by_id["Fc"]

    # Dependency-driven: Fc must start well before S finishes.
    # Use a generous margin to avoid flakiness: Fc should start at least
    # SLOW_SLEEP/2 before S is done.
    margin = _SLOW_SLEEP / 2
    assert fc_start < s_finish - margin, (
        f"Fc started at {fc_start:.4f}, S finishes ~{s_finish:.4f}; "
        f"expected Fc to start at least {margin:.2f}s before S finishes "
        "(dependency-driven scheduler broken)"
    )


def test_fc_depends_only_on_f_not_s() -> None:
    """F must complete before Fc starts."""
    fleet_config = load_fleet_config(ROOT / "fleet.example.yaml")
    resolver = YamlPersonaResolver(fleet_config)
    dispatcher = _TimedDispatcher(
        sleep_map={"F": _FAST_SLEEP, "S": _SLOW_SLEEP, "Fc": _FAST_SLEEP}
    )
    parent = FleetTask(goal="parent", persona="coder", pipeline="simple")

    dispatch_dag(
        spec=_two_chain_spec(),
        parent_task=parent,
        dispatcher=cast("object", dispatcher),  # type: ignore[arg-type]
        persona_resolver=resolver,
        fallback_persona="coder",
        default_pipeline="simple",
    )

    by_id = dict(dispatcher.events)
    f_finish = by_id["F"] + _FAST_SLEEP
    fc_start = by_id["Fc"]

    # Fc must not start before F finishes (within scheduling overhead).
    assert fc_start >= f_finish - 0.05, (
        f"Fc started at {fc_start:.4f} but F only finishes ~{f_finish:.4f}"
    )
