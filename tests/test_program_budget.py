"""Pin the E2 token-budget behavior of run_workflow_program."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.program import run_workflow_program


@dataclass
class _Dispatcher:
    """Returns a fixed summary for every agent, recording goals thread-safely."""

    summary: str = "ok"
    goals: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,  # noqa: ARG002
        same_workspace_tasks: int = 1,  # noqa: ARG002
        handoff: object = None,  # noqa: ARG002
        base_branch: str | None = None,  # noqa: ARG002
        depth: int = 1,  # noqa: ARG002
    ) -> FleetTaskResult:
        with self._lock:
            self.goals.append(task.goal)
        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status="completed",
            summary=self.summary,
            error=None,
            duration_seconds=0.0,
            observed_total_tokens=100,
        )


# ---------------------------------------------------------------------------
# (a) self-throttle loop
# ---------------------------------------------------------------------------

_SELF_THROTTLE = (
    "n = 0\n"
    "while budget.remaining() > 0:\n"
    "    agent('s')\n"
    "    n += 1\n"
    "return n"
)


def test_self_throttle_loop_dispatches_until_budget_exhausted() -> None:
    summary = run_workflow_program(_SELF_THROTTLE, dispatcher=_Dispatcher(), token_budget=250)
    assert summary.status == "completed"
    assert summary.result == 3
    assert summary.agents_dispatched == 3


# ---------------------------------------------------------------------------
# (b) hard ceiling refuses extra dispatch
# ---------------------------------------------------------------------------

_THREE_SEQUENTIAL = (
    "a = agent('a')\n"
    "b = agent('b')\n"
    "c = agent('c')\n"
    "return [a.summary, b.summary, c.summary]"
)


def test_hard_ceiling_refuses_third_dispatch() -> None:
    # spent(200) >= 150 so c's _dispatch raises; since no schema-retry last is
    # None inside that agent() call, it propagates -> status="error".
    # Two agents returned before the ceiling fired.
    summary = run_workflow_program(_THREE_SEQUENTIAL, dispatcher=_Dispatcher(), token_budget=150)
    assert summary.agents_dispatched == 2
    assert summary.status == "error"
    assert summary.error is not None
    assert "token budget" in summary.error


# ---------------------------------------------------------------------------
# (c) total readback
# ---------------------------------------------------------------------------

def test_budget_total_readable_from_program() -> None:
    summary = run_workflow_program(
        "x = agent('x')\nreturn budget.total",
        dispatcher=_Dispatcher(),
        token_budget=500,
    )
    assert summary.result == 500


# ---------------------------------------------------------------------------
# (d) unbounded: token_budget=None
# ---------------------------------------------------------------------------

def test_budget_remaining_is_inf_when_unbounded() -> None:
    summary = run_workflow_program(
        "x = agent('x')\nreturn budget.remaining()",
        dispatcher=_Dispatcher(),
        token_budget=None,
    )
    assert summary.result == float("inf")


def test_budget_total_is_none_when_unbounded() -> None:
    summary = run_workflow_program(
        "x = agent('x')\nreturn budget.total",
        dispatcher=_Dispatcher(),
        token_budget=None,
    )
    assert summary.result is None


# ---------------------------------------------------------------------------
# (e) spent counts returned agents
# ---------------------------------------------------------------------------

def test_budget_spent_counts_returned_agents() -> None:
    summary = run_workflow_program(
        "a = agent('a')\nb = agent('b')\nreturn budget.spent()",
        dispatcher=_Dispatcher(),
        token_budget=None,
    )
    assert summary.result == 200
