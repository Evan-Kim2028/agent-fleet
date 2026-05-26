"""Shared agent prompt assembly for execute and fix dispatch paths."""

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
