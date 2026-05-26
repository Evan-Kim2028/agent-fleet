"""Shared agent prompt assembly for persona dispatches.

Every ``backend.run()`` / ``session.send()`` path that represents a persona doing
work should build its prompt via :func:`build_agent_prompt` with
``persona_body=equip.compose_body`` (from :func:`resolve_dispatch_equip` or
``task.equip``) when equip is available.

In-scope call sites (this package):

- ``phases.run_execute_phase`` — equip compose body + ``build_agent_prompt``
- ``phases._legacy_review_phase`` — reviewer persona + review skills in context
- ``code_review.fix.run_fix_phase`` — equip via ``_resolve_fix_equip`` + ``build_agent_prompt``

Out-of-scope but audited (must stay aligned when touched elsewhere):

- ``pr_loop.lifecycle`` — review/CI fix agents (equip + ``build_agent_prompt``)
- ``reviewer.review`` — structured review; review skills via ``task_context``
- ``planner`` / ``implementer`` / ``researcher`` / scouts — pipeline-specific, no equip yet
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentPrompt:
    full: str
    persona_section: str
    task_section: str


def build_agent_prompt(
    *,
    persona_body: str,
    task_heading: str,
    task_body: str,
    context: str = "",
    extra_instructions: str = "",
    allowed_paths: tuple[str, ...] | list[str] = (),
    closing_instruction: str = "",
    extra_sections: list[tuple[str, str]] | None = None,
) -> AgentPrompt:
    """Layer persona (skills+stub+overlays) then structured task sections."""
    persona_parts = [
        "# Persona",
        persona_body.strip(),
    ]
    if extra_instructions.strip():
        persona_parts.extend(["", "# Additional Instructions", extra_instructions.strip()])
    if allowed_paths:
        paths = ", ".join(allowed_paths)
        persona_parts.extend(["", f"# Scope: only modify paths matching: {paths}"])
    persona_section = "\n".join(persona_parts)

    task_parts: list[str] = ["", f"# {task_heading}", task_body.strip()]
    if context.strip():
        task_parts.extend(["", "# Context", context.strip()])
    for heading, body in extra_sections or []:
        if body.strip():
            task_parts.extend(["", f"# {heading}", body.strip()])
    if closing_instruction.strip():
        task_parts.extend(["", closing_instruction.strip()])
    task_section = "\n".join(task_parts)

    full = f"{persona_section}{task_section}"
    return AgentPrompt(full=full, persona_section=persona_section, task_section=task_section)
