"""Shared prompt assembly for persona-backed agent dispatch."""

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
    extra_sections: list[tuple[str, str]] | None = None,
) -> AgentPrompt:
    """Layer persona (skills+stub+overlays) then structured task sections."""
    persona_section = "\n".join(["# Persona", persona_body.strip()])

    parts: list[str] = []
    for heading, body in extra_sections or []:
        if body.strip():
            parts.extend(["", f"# {heading}", body.strip()])
        else:
            parts.extend(["", f"# {heading}"])

    parts.extend(["", f"# {task_heading}", task_body.strip()])

    if context.strip():
        parts.extend(["", "# Context", context.strip()])

    task_section = "\n".join(parts)
    full = persona_section + task_section
    return AgentPrompt(full=full, persona_section=persona_section, task_section=task_section)
