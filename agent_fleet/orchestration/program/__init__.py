"""LLM-generated orchestration programs.

The planner writes a short Python program per task. It calls five primitives,
``agent`` / ``parallel`` / ``pipeline`` / ``phase`` / ``log``, to dispatch fleet
subagents, fan them out, and converge their work into one answer. The
coordination logic lives in that program, not in any model's context, and each
subagent's transcript stays inside the subagent.

Public surface:

- ``run_workflow_program(source, *, dispatcher, ...) -> ProgramRunSummary``
- ``validate_workflow_program(source) -> ProgramValidation``
- the data shapes ``AgentResult`` and ``ProgramRunSummary``
- the error types ``WorkflowProgramError`` and subclasses
"""

from __future__ import annotations

from agent_fleet.orchestration.program.models import (
    AgentResult,
    ProgramExecutionError,
    ProgramRunSummary,
    ProgramValidation,
    ProgramValidationError,
    WorkflowProgramError,
)
from agent_fleet.orchestration.program.runtime import run_workflow_program
from agent_fleet.orchestration.program.validate import validate_workflow_program

__all__ = [
    "AgentResult",
    "ProgramExecutionError",
    "ProgramRunSummary",
    "ProgramValidation",
    "ProgramValidationError",
    "WorkflowProgramError",
    "run_workflow_program",
    "validate_workflow_program",
]
