"""Fix Attempt memory seam — structured state for VERIFY/FIX retry loops.

FixMemory carries the per-attempt context (diff snapshot, accumulated failure
messages, files touched) across iterations.  Two concrete strategies implement
FixStrategy: ColdRestartStrategy (the default) reproduces the existing
dispose+recreate behaviour verbatim; WarmContinuationStrategy keeps the
session alive and feeds the structured FixMemory into the prompt.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from agent_fleet.fleet_session import create_fleet_session
from agent_fleet.implementer import implement
from agent_fleet.synthesizer import synthesize

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.contracts.implementation_brief import ImplementationBrief
    from agent_fleet.contracts.task_spec import TaskSpec
    from agent_fleet.hooks import (
        LLMBackend,
        LLMSession,
        PersonaResolver,
    )


def _truncate(message: str, *, max_lines: int = 50) -> str:
    if not message:
        return message
    lines = message.splitlines()
    if len(lines) <= max_lines:
        return message
    omitted = len(lines) - max_lines
    return "\n".join(lines[:max_lines]) + f"\n... [{omitted} more lines truncated]"


@dataclass(frozen=True)
class FixMemory:
    """Snapshot of accumulated fix-loop state at each attempt boundary."""

    attempt: int
    diff_so_far: str
    failures: tuple[str, ...]
    files_touched: tuple[str, ...]


@dataclass
class _FixDeps:
    """Bundle of runner-level services forwarded to each strategy.

    Exists purely so strategies don't need positional-arg explosion.
    """

    backend: LLMBackend
    persona_resolver: PersonaResolver
    fleet_config: Any | None
    persona: str
    repo_root: Path
    require_mcp: bool
    compose_body: str | None


class FixStrategy(Protocol):
    """Strategy for the FIX phase of the VERIFY/FIX retry loop."""

    def run_fix(
        self,
        mem: FixMemory,
        *,
        task_spec: TaskSpec,
        worktree: Path,
        branch: str,
        deps: _FixDeps,
        notes: list[Any],
        session: LLMSession | None,
        brief: ImplementationBrief | None,
    ) -> tuple[ImplementationBrief, LLMSession | None]:
        """Execute one FIX iteration and return (new_brief, updated_session)."""
        ...


class ColdRestartStrategy:
    """Dispose the current session, create a fresh one, re-synthesize + implement.

    This is an exact extraction of the existing FIX block behaviour.  Selecting
    this strategy must produce identical observable results to the pre-C3 code.
    """

    def run_fix(
        self,
        mem: FixMemory,
        *,
        task_spec: TaskSpec,
        worktree: Path,
        branch: str,
        deps: _FixDeps,
        notes: list[Any],
        session: LLMSession | None,
        brief: ImplementationBrief | None,
    ) -> tuple[ImplementationBrief, LLMSession | None]:
        del brief
        if session is not None:
            with contextlib.suppress(Exception):
                session.dispose()

        new_session = create_fleet_session(
            deps.backend,
            fleet_config=deps.fleet_config,
            persona_resolver=deps.persona_resolver,
            persona=deps.persona,
            cwd=deps.repo_root,
        )

        verify_msg = _truncate(mem.failures[-1] if mem.failures else "")
        new_brief = synthesize(
            task_spec,
            notes,
            backend=deps.backend,
            extra_context=f"Verification failed: {verify_msg}. Fix and retry.",
            session=new_session,
        )
        implement(
            new_brief,
            task_spec,
            worktree,
            branch,
            backend=deps.backend,
            persona_resolver=deps.persona_resolver,
            persona_name=deps.persona,
            prompt_suffix=f"Previous verify failure: {verify_msg}",
            session=new_session,
            require_mcp_tools=deps.require_mcp,
            compose_body=deps.compose_body,
        )
        return new_brief, new_session


class WarmContinuationStrategy:
    """Keep the existing session alive; feed FixMemory into the prompt.

    The session is NOT disposed.  Instead the structured diff+failure history
    is injected as an extra_context string so the agent can reason about the
    accumulated state without losing its conversation context.

    Gated behind ``FleetRunConfig.fix_strategy == "warm"``; not the default.
    """

    def run_fix(
        self,
        mem: FixMemory,
        *,
        task_spec: TaskSpec,
        worktree: Path,
        branch: str,
        deps: _FixDeps,
        notes: list[Any],
        session: LLMSession | None,
        brief: ImplementationBrief | None,
    ) -> tuple[ImplementationBrief, LLMSession | None]:
        del brief
        failures_text = "\n---\n".join(mem.failures)
        extra = (
            f"Fix attempt {mem.attempt}.\n"
            f"Files touched so far: {', '.join(mem.files_touched) or 'none'}.\n"
            f"Diff so far:\n{mem.diff_so_far}\n"
            f"Accumulated failures:\n{failures_text}"
        )
        last_msg = _truncate(mem.failures[-1] if mem.failures else "")
        new_brief = synthesize(
            task_spec,
            notes,
            backend=deps.backend,
            extra_context=extra,
            session=session,
        )
        implement(
            new_brief,
            task_spec,
            worktree,
            branch,
            backend=deps.backend,
            persona_resolver=deps.persona_resolver,
            persona_name=deps.persona,
            prompt_suffix=f"Previous verify failure: {last_msg}",
            session=session,
            require_mcp_tools=deps.require_mcp,
            compose_body=deps.compose_body,
        )
        return new_brief, session


def make_fix_strategy(name: str) -> FixStrategy:
    """Construct a FixStrategy by name; raises ValueError for unknown names."""
    if name == "cold":
        return ColdRestartStrategy()
    if name == "warm":
        return WarmContinuationStrategy()
    raise ValueError(f"unknown fix_strategy {name!r}; expected 'cold' or 'warm'")
