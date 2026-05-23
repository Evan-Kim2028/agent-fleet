"""Cursor agent mode literals and validation."""

from __future__ import annotations

from typing import Literal

AgentMode = Literal["agent", "plan"]


def coerce_agent_mode(value: str | None, *, default: AgentMode = "agent") -> AgentMode:
    selected = default if value is None else value
    if selected == "agent":
        return "agent"
    if selected == "plan":
        return "plan"
    raise ValueError(f"Invalid agent mode {selected!r}; expected 'agent' or 'plan'")


def parse_agent_mode(value: str | None) -> AgentMode | None:
    if value is None:
        return None
    return coerce_agent_mode(value)
