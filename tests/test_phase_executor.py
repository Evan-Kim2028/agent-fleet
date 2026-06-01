"""execute_graph: graph walk, condition gating, terminal routing."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_fleet.phase_graph import (
    PhaseDeps,
    PhaseGraph,
    PhaseHandler,
    PhaseResult,
    PhaseRunContext,
    PhaseSpec,
    execute_graph,
)
from agent_fleet.runner import FleetRunResult


def _stub_result(outcome: str = "completed") -> FleetRunResult:
    return FleetRunResult(run_id="x", task_id=1, persona="coder", outcome=outcome)


def _stub_deps() -> PhaseDeps:
    return PhaseDeps(
        backend=MagicMock(),
        persona_resolver=MagicMock(),
        git_ops=MagicMock(),
        verifier=MagicMock(),
        forge=None,
        run_config=MagicMock(),
        spine=MagicMock(),
        run_log=MagicMock(),
        fix_strategy=MagicMock(),
        controller=MagicMock(),
        controller_policy=MagicMock(),
        disposition_policy=MagicMock(),
    )


class _RecordingHandler(PhaseHandler):
    def __init__(self, terminal: FleetRunResult | None = None) -> None:
        self.calls: list[tuple[PhaseRunContext, PhaseDeps]] = []
        self._terminal = terminal

    def run(self, ctx: PhaseRunContext, deps: PhaseDeps) -> PhaseResult:
        self.calls.append((ctx, deps))
        return PhaseResult(terminal=self._terminal)


def _simple_graph(names: list[str]) -> tuple[PhaseGraph, dict[str, _RecordingHandler]]:
    specs = [PhaseSpec(name=n, handler_key=n.lower()) for n in names]
    graph = PhaseGraph(specs)
    handlers: dict[str, _RecordingHandler] = {n.lower(): _RecordingHandler() for n in names}
    return graph, handlers


class _VisitingHandler(PhaseHandler):
    """Handler that appends its key to a shared list on each run."""

    def __init__(self, key: str, log: list[str]) -> None:
        self.calls: list[tuple[PhaseRunContext, PhaseDeps]] = []
        self._key = key
        self._log = log

    def run(self, ctx: PhaseRunContext, deps: PhaseDeps) -> PhaseResult:
        self._log.append(self._key)
        self.calls.append((ctx, deps))
        return PhaseResult(terminal=None)


def test_execute_graph_visits_all_phases_in_order() -> None:
    names = ["PLAN", "RESEARCH", "IMPLEMENT"]
    visited: list[str] = []
    specs = [PhaseSpec(name=n, handler_key=n.lower()) for n in names]
    graph = PhaseGraph(specs)
    handlers: dict[str, _VisitingHandler] = {
        n.lower(): _VisitingHandler(n.lower(), visited) for n in names
    }

    result = execute_graph(graph, PhaseRunContext(), handlers, deps=_stub_deps())
    assert result is None
    assert visited == ["plan", "research", "implement"]


def test_execute_graph_stops_at_first_terminal() -> None:
    terminal = _stub_result("verify_failed")
    specs = [
        PhaseSpec(name="VERIFY", handler_key="verify"),
        PhaseSpec(name="REVIEW", handler_key="review", depends_on=("VERIFY",)),
    ]
    graph = PhaseGraph(specs)
    verify_h = _RecordingHandler(terminal=terminal)
    review_h = _RecordingHandler()
    handlers: dict[str, PhaseHandler] = {"verify": verify_h, "review": review_h}

    out = execute_graph(graph, PhaseRunContext(), handlers, deps=_stub_deps())
    assert out is terminal
    assert len(verify_h.calls) == 1
    assert len(review_h.calls) == 0


def test_execute_graph_skips_false_condition() -> None:
    specs = [
        PhaseSpec(name="ALWAYS", handler_key="always"),
        PhaseSpec(
            name="CONDITIONAL",
            handler_key="conditional",
            depends_on=("ALWAYS",),
            condition=lambda _ctx: False,
        ),
        PhaseSpec(name="AFTER", handler_key="after", depends_on=("ALWAYS",)),
    ]
    graph = PhaseGraph(specs)
    always_h = _RecordingHandler()
    cond_h = _RecordingHandler()
    after_h = _RecordingHandler()
    handlers: dict[str, PhaseHandler] = {
        "always": always_h,
        "conditional": cond_h,
        "after": after_h,
    }
    execute_graph(graph, PhaseRunContext(), handlers, deps=_stub_deps())
    assert len(always_h.calls) == 1
    assert len(cond_h.calls) == 0
    assert len(after_h.calls) == 1


def test_execute_graph_skips_missing_handler() -> None:
    graph, handlers = _simple_graph(["PLAN", "RESEARCH"])
    del handlers["research"]
    result = execute_graph(graph, PhaseRunContext(), handlers, deps=_stub_deps())
    assert result is None
    assert len(handlers["plan"].calls) == 1


def test_execute_graph_returns_none_when_exhausted() -> None:
    graph, handlers = _simple_graph(["PLAN"])
    result = execute_graph(graph, PhaseRunContext(), handlers, deps=_stub_deps())
    assert result is None


def test_execute_graph_routes_terminal_via_disposition() -> None:
    scope_result = _stub_result("scope_violation_salvaged")
    specs = [
        PhaseSpec(name="IMPLEMENT", handler_key="implement"),
        PhaseSpec(name="VERIFY", handler_key="verify", depends_on=("IMPLEMENT",)),
    ]
    graph = PhaseGraph(specs)
    implement_h = _RecordingHandler(terminal=scope_result)
    verify_h = _RecordingHandler()
    handlers: dict[str, PhaseHandler] = {"implement": implement_h, "verify": verify_h}

    out = execute_graph(graph, PhaseRunContext(), handlers, deps=_stub_deps())
    assert out is scope_result
    assert len(verify_h.calls) == 0


def test_phase_deps_fields() -> None:
    deps = _stub_deps()
    assert deps.forge is None
    assert deps.backend is not None
