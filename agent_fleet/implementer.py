"""Implementer phase module.

Loads a persona prompt, builds a structured prompt from the ImplementationBrief
and TaskSpec, then invokes an LLMBackend to perform the implementation work.

The implementer does NOT manage worktree lifecycle — that is FleetRunner's job.
Actual diff-application is the persona's responsibility (the LLM writes files
inside the worktree directly).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.contracts.implementation_brief import ImplementationBrief
    from agent_fleet.contracts.task_spec import TaskSpec
    from agent_fleet.hooks import LLMBackend, LLMResult, LLMSession, PersonaResolver

_DEFAULT_PERSONA_PROMPT = "You are a helpful coding assistant."


def implement(
    brief: ImplementationBrief,
    task_spec: TaskSpec,
    worktree_path: Path,
    _branch_name: str,
    *,
    backend: LLMBackend,
    persona_resolver: PersonaResolver,
    persona_name: str,
    max_tokens: int = 8192,
    timeout_s: int = 1800,
    memory_limit: str = "4G",
    prompt_suffix: str | None = None,
    session: LLMSession | None = None,
) -> LLMResult:
    """Run the Implementer phase.

    Loads the persona prompt via *persona_resolver*, builds a structured prompt
    from *brief* and *task_spec* (allowed_paths, forbidden_paths, acceptance_criteria,
    files_to_create, files_to_modify, test_strategy), then calls *backend.run()*.

    The LLM runs inside *worktree_path* (already set up by FleetRunner). This
    function does NOT manage the worktree lifecycle — that is FleetRunner's job.

    Returns the LLMResult so callers can surface stdout/stderr on the no-diff
    diagnostic path. Raises RuntimeError if backend.run() returns exit_code != 0.
    """
    # TODO(PR D): admit via fleet.admission.AdmissionController
    persona = persona_resolver.load(persona_name)

    # Load persona prompt text; fall back to default if path doesn't exist on disk.
    try:
        persona_prompt = persona.prompt_path.read_text()
    except (FileNotFoundError, OSError):
        persona_prompt = _DEFAULT_PERSONA_PROMPT

    allowed_tools = persona.allowed_tools

    prompt = _build_prompt(
        persona_prompt=persona_prompt,
        brief=brief,
        task_spec=task_spec,
        worktree_path=worktree_path,
        prompt_suffix=prompt_suffix,
    )

    if session is not None:
        result = session.send(
            prompt,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            allowed_tools=allowed_tools,
        )
    else:
        result = backend.run(
            prompt,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            memory_limit=memory_limit,
            allowed_tools=allowed_tools,
            cwd=worktree_path,
        )

    if result.exit_code != 0:
        raise RuntimeError(
            f"implementer LLM exited {result.exit_code}: {result.stderr[:200]}"
        )

    return result


def _build_prompt(
    *,
    persona_prompt: str,
    brief: ImplementationBrief,
    task_spec: TaskSpec,
    worktree_path: Path,
    prompt_suffix: str | None = None,
) -> str:
    """Assemble the full structured prompt for the implementer LLM."""
    brief_dict = brief.to_dict()
    allowed_paths = task_spec.scope.allowed_paths
    forbidden_paths = task_spec.scope.forbidden_paths
    acceptance_criteria = task_spec.acceptance_criteria

    sections = [
        persona_prompt.strip(),
        "",
        "## Implementation Brief",
        json.dumps(brief_dict, indent=2),
        "",
        "## Scope",
        "### Allowed paths",
        "\n".join(f"- {p}" for p in allowed_paths) if allowed_paths else "(none)",
        "",
        "### Forbidden paths",
        "\n".join(f"- {p}" for p in forbidden_paths) if forbidden_paths else "(none)",
        "",
        "## Acceptance Criteria",
        "\n".join(f"- {c}" for c in acceptance_criteria)
        if acceptance_criteria
        else "(none)",
        "",
        "## Worktree",
        str(worktree_path),
        "",
        "## Instructions",
        "- Make all file changes directly in the worktree.",
        "- Do NOT run `git commit` or `git push` — the runner will handle that.",
    ]

    if prompt_suffix:
        sections += ["", "## Reminder", prompt_suffix]

    return "\n".join(sections)
