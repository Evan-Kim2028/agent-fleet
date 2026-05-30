"""Stretch tests for dispatch_dag via InstrumentedDispatcher.

Each test asserts a real correctness property AND records timing/token metrics.
No real LLM or composer is called — all dispatch is in-process and deterministic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_fleet.hooks import FleetTask, FleetTaskResult, Persona
from agent_fleet.orchestration.dag.runner import dispatch_dag
from agent_fleet.orchestration.dag.schema import DagSpec, DagTask

# ---------------------------------------------------------------------------
# Minimal stub PersonaResolver (no YAML, no filesystem)
# ---------------------------------------------------------------------------


class _StubPersonaResolver:
    def list_personas(self) -> list[str]:
        return ["coder"]

    def load(self, name: str, *, loadout_size: str | None = None) -> Persona:
        raise NotImplementedError


_RESOLVER = _StubPersonaResolver()
_PARENT = FleetTask(goal="parent", persona="coder", pipeline="simple")

# ---------------------------------------------------------------------------
# Timing-aware dispatcher for DAG tests
# ---------------------------------------------------------------------------


@dataclass
class _TimedDispatcher:
    sleep_map: dict[str, float] = field(default_factory=dict)
    default_sleep: float = 0.01
    events: list[tuple[str, float, float]] = field(default_factory=list)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        **_: object,
    ) -> FleetTaskResult:
        node_id = (task.title or "").rsplit(" — ", 1)[-1]
        t0 = time.monotonic()
        sleep_t = self.sleep_map.get(node_id, self.default_sleep)
        time.sleep(sleep_t)
        t1 = time.monotonic()
        self.events.append((node_id, t0, t1))
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=f"done {node_id}",
            error=None,
            duration_seconds=t1 - t0,
        )


@dataclass
class _FailingDispatcher:
    fail_ids: set[str] = field(default_factory=set)
    default_sleep: float = 0.01

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        **_: object,
    ) -> FleetTaskResult:
        node_id = (task.title or "").rsplit(" — ", 1)[-1]
        time.sleep(self.default_sleep)
        if node_id in self.fail_ids:
            return FleetTaskResult(
                task_index=task_index,
                persona=task.persona,
                goal=task.goal,
                status="error",
                summary=None,
                error=f"injected failure for {node_id}",
                duration_seconds=self.default_sleep,
            )
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=f"done {node_id}",
            error=None,
            duration_seconds=self.default_sleep,
        )


# ---------------------------------------------------------------------------
# Scenario 5 — wide_deep_dag
# Multiple ranks, a wide fanout rank, cross-rank deps.
# Assert all results present, none duplicated.
# ---------------------------------------------------------------------------


def _wide_deep_spec() -> DagSpec:
    """DAG shape:
    rank 0:  root-A, root-B
    rank 1:  wide-0 .. wide-7  (each depends on root-A)
             side-0, side-1    (each depends on root-B)
    rank 2:  merge             (depends on all wide-* and both side-*)
    rank 3:  final             (depends on merge)
    """
    tasks: list[DagTask] = [
        DagTask(id="root-A", depends_on=(), complexity="LOW", subtask_prompt="root A"),
        DagTask(id="root-B", depends_on=(), complexity="LOW", subtask_prompt="root B"),
    ]
    wide_ids = [f"wide-{i}" for i in range(8)]
    for wid in wide_ids:
        tasks.append(DagTask(id=wid, depends_on=("root-A",), complexity="LOW", subtask_prompt=wid))
    side_ids = ["side-0", "side-1"]
    for sid in side_ids:
        tasks.append(DagTask(id=sid, depends_on=("root-B",), complexity="LOW", subtask_prompt=sid))
    merge_deps = tuple(wide_ids + side_ids)
    tasks.append(
        DagTask(id="merge", depends_on=merge_deps, complexity="LOW", subtask_prompt="merge")
    )
    tasks.append(
        DagTask(id="final", depends_on=("merge",), complexity="LOW", subtask_prompt="final")
    )
    return DagSpec(title="wide-deep", tasks=tuple(tasks))


def test_wide_deep_dag() -> None:
    dispatcher = _TimedDispatcher(default_sleep=0.01)
    spec = _wide_deep_spec()

    summary = dispatch_dag(
        spec=spec,
        parent_task=_PARENT,
        dispatcher=dispatcher,
        persona_resolver=_RESOLVER,
        fallback_persona="coder",
        default_pipeline="simple",
    )

    assert summary.aggregate_status == "completed", summary.error

    expected_ids = {t.id for t in spec.tasks}
    result_goals = [r.goal for r in summary.results]
    result_node_ids = {g.rsplit(" — ", 1)[-1] for g in result_goals}

    assert result_node_ids == expected_ids, (
        f"missing or extra nodes: got={result_node_ids}, want={expected_ids}"
    )

    assert len(result_goals) == len(set(result_goals)), "duplicate goals in results"

    statuses = {r.goal.rsplit(" — ", 1)[-1]: r.status for r in summary.results}
    for node_id in expected_ids:
        assert statuses[node_id] == "completed", f"node {node_id} has status {statuses[node_id]!r}"


# ---------------------------------------------------------------------------
# Scenario 6 — dependency_driven_timing
# A wider/deeper version of the basic dep-driven test.
# Fast chain: root-F -> mid-F -> leaf-F  (all fast)
# Slow branch: root-S (slow, no dependents)
# Assert leaf-F.start < root-S.end (proves per-task launch, not rank barrier).
# ---------------------------------------------------------------------------

_FAST = 0.02
_SLOW = 0.25


def _dep_driven_spec() -> DagSpec:
    """Shape:
    root-F  -> mid-F  -> leaf-F   (fast chain)
    root-S                         (slow sibling in rank-0, no deps on it)
    root-X -> mid-X               (third independent chain, medium)
    """
    return DagSpec(
        title="dep-driven",
        tasks=(
            DagTask(id="root-F", depends_on=(), complexity="LOW", subtask_prompt="fast root"),
            DagTask(id="root-S", depends_on=(), complexity="LOW", subtask_prompt="slow root"),
            DagTask(id="root-X", depends_on=(), complexity="LOW", subtask_prompt="x root"),
            DagTask(
                id="mid-F", depends_on=("root-F",), complexity="LOW", subtask_prompt="fast mid"
            ),
            DagTask(id="mid-X", depends_on=("root-X",), complexity="LOW", subtask_prompt="x mid"),
            DagTask(
                id="leaf-F", depends_on=("mid-F",), complexity="LOW", subtask_prompt="fast leaf"
            ),
        ),
    )


def test_dependency_driven_timing() -> None:
    dispatcher = _TimedDispatcher(
        sleep_map={
            "root-F": _FAST,
            "mid-F": _FAST,
            "leaf-F": _FAST,
            "root-S": _SLOW,
            "root-X": _FAST,
            "mid-X": _FAST,
        },
        default_sleep=_FAST,
    )

    summary = dispatch_dag(
        spec=_dep_driven_spec(),
        parent_task=_PARENT,
        dispatcher=dispatcher,
        persona_resolver=_RESOLVER,
        fallback_persona="coder",
        default_pipeline="simple",
    )

    assert summary.aggregate_status == "completed", summary.error

    by_id: dict[str, tuple[float, float]] = {e[0]: (e[1], e[2]) for e in dispatcher.events}
    assert "leaf-F" in by_id, f"leaf-F not found in events: {list(by_id.keys())}"
    assert "root-S" in by_id, f"root-S not found in events: {list(by_id.keys())}"

    leaf_f_end = by_id["leaf-F"][1]
    root_s_end = by_id["root-S"][1]

    assert leaf_f_end < root_s_end, (
        f"leaf-F ended at {leaf_f_end:.4f} but root-S ended at {root_s_end:.4f}; "
        "expected leaf-F to finish before the slow sibling (dependency-driven broken)"
    )


# ---------------------------------------------------------------------------
# Scenario 7 — skip_propagation
# A mid-level task fails; its transitive dependents are skipped;
# independent branches complete normally.
# ---------------------------------------------------------------------------


def _skip_propagation_spec() -> DagSpec:
    """Shape:
    root-ok    -> mid-fail -> child-A -> grandchild-A
                           -> child-B
    root-good  -> good-mid -> good-leaf
    """

    def _t(nid: str, deps: tuple[str, ...], prompt: str) -> DagTask:
        return DagTask(id=nid, depends_on=deps, complexity="LOW", subtask_prompt=prompt)

    return DagSpec(
        title="skip-prop",
        tasks=(
            _t("root-ok", (), "root ok"),
            _t("mid-fail", ("root-ok",), "mid fail"),
            _t("child-A", ("mid-fail",), "child A"),
            _t("grandchild-A", ("child-A",), "grandchild A"),
            _t("child-B", ("mid-fail",), "child B"),
            _t("root-good", (), "root good"),
            _t("good-mid", ("root-good",), "good mid"),
            _t("good-leaf", ("good-mid",), "good leaf"),
        ),
    )


def test_skip_propagation() -> None:
    dispatcher = _FailingDispatcher(fail_ids={"mid-fail"}, default_sleep=0.01)

    summary = dispatch_dag(
        spec=_skip_propagation_spec(),
        parent_task=_PARENT,
        dispatcher=dispatcher,
        persona_resolver=_RESOLVER,
        fallback_persona="coder",
        default_pipeline="simple",
    )

    assert summary.aggregate_status != "completed", "expected non-completed due to mid-fail failure"

    statuses: dict[str, str] = {}
    for r in summary.results:
        node_id = r.goal.rsplit(" — ", 1)[-1]
        statuses[node_id] = r.status

    assert statuses.get("root-ok") == "completed"
    assert statuses.get("mid-fail") == "error"

    for skipped_id in ("child-A", "grandchild-A", "child-B"):
        assert statuses.get(skipped_id) == "skipped", (
            f"expected {skipped_id} to be skipped, got {statuses.get(skipped_id)!r}"
        )

    for completed_id in ("root-good", "good-mid", "good-leaf"):
        assert statuses.get(completed_id) == "completed", (
            f"expected {completed_id} to be completed, got {statuses.get(completed_id)!r}"
        )
