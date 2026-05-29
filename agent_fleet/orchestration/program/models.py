"""Data shapes for LLM-generated orchestration programs.

A workflow program is Python source the planner writes for one task. It calls
the primitives in ``runtime.py`` to dispatch subagents, fan them out, and
converge their work into a single answer. The shapes here are the interface
between that program, the runner that executes it, and the parent fleet that
receives the result.

The load-bearing idea is context isolation. Each dispatched subagent has its
own context window. Its full transcript stays inside the agent. Only a bounded
``AgentResult`` crosses back into the program, and only the program's final
return value plus a small ``ProgramRunSummary`` crosses back to the parent. A
twenty-agent audit can burn six hundred thousand tokens across agents and still
hand the parent a two thousand token answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_OK_STATUSES = frozenset({"completed", "merged", "review_changes_requested"})


class WorkflowProgramError(Exception):
    """Base error for the workflow-program subsystem."""


class ProgramValidationError(WorkflowProgramError):
    """The program source failed AST validation before any execution."""


class ProgramExecutionError(WorkflowProgramError):
    """The program raised, exceeded its agent budget, or ran past its deadline."""


@dataclass(frozen=True)
class AgentResult:
    """The bounded result of one dispatched subagent.

    This is everything the program sees of a subagent. The subagent's full
    transcript never enters the program's namespace. ``summary`` is the
    synthesized answer the agent returned, already truncated to a ceiling.
    ``data`` holds parsed structured output when the call requested a schema.
    ``schema_error`` is None when ``data`` validated against the requested
    schema, or a human-readable reason when it did not (after the one retry).
    """

    status: str
    summary: str
    persona: str
    goal: str
    duration_seconds: float = 0.0
    agent_id: str | None = None
    data: dict[str, object] | None = None
    schema_error: str | None = None
    error: str | None = None
    observed_total_tokens: int | None = None
    task_index: int = -1

    @property
    def ok(self) -> bool:
        return self.status in _OK_STATUSES

    def __str__(self) -> str:
        return self.summary or ""

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class ProgramValidation:
    """The verdict of static validation. Pure, no dispatch."""

    ok: bool
    errors: tuple[str, ...] = ()
    agent_calls: int = 0
    uses_parallel: bool = False
    uses_pipeline: bool = False
    uses_dynamic: bool = False

    def raise_if_invalid(self) -> None:
        if not self.ok:
            raise ProgramValidationError("; ".join(self.errors) or "invalid program")


@dataclass(frozen=True)
class ProgramRunSummary:
    """What the parent fleet receives back from one workflow program.

    ``result`` is the program's return value, the synthesized answer.
    ``agent_results`` is kept for the audit trail and is deliberately NOT meant
    to be fed back into any LLM context. The two token counts are the headline
    measurement: ``tokens_across_agents`` is the work done, ``tokens_to_parent``
    is what the parent has to read.
    """

    status: str
    result: object = None
    error: str | None = None
    agents_dispatched: int = 0
    agents_ok: int = 0
    phases: tuple[str, ...] = ()
    log: tuple[str, ...] = ()
    tokens_across_agents: int = 0
    tokens_to_parent: int = 0
    duration_seconds: float = 0.0
    agent_results: tuple[AgentResult, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    @property
    def context_leverage(self) -> float:
        """Tokens worked per token the parent must read. Higher is better."""
        if self.tokens_to_parent <= 0:
            return float(self.tokens_across_agents)
        return self.tokens_across_agents / self.tokens_to_parent

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "agents_dispatched": self.agents_dispatched,
            "agents_ok": self.agents_ok,
            "phases": list(self.phases),
            "log": list(self.log),
            "tokens_across_agents": self.tokens_across_agents,
            "tokens_to_parent": self.tokens_to_parent,
            "context_leverage": round(self.context_leverage, 1),
            "duration_seconds": round(self.duration_seconds, 2),
        }
