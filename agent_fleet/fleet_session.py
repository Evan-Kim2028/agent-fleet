"""Shared session factory for fleet backends."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.hooks import SessionCapableBackend

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import LLMBackend, LLMSession, PersonaResolver


def create_fleet_session(
    backend: LLMBackend,
    *,
    fleet_config: FleetConfig | None,
    persona_resolver: PersonaResolver,
    persona: str,
    cwd: Path,
) -> LLMSession | None:
    """Open one MCP-aware session when the backend supports it."""
    if fleet_config is None or not isinstance(backend, SessionCapableBackend):
        return None
    persona_spec = persona_resolver.load(persona)
    mcp_specs = {
        name: fleet_config.mcp_servers[name]
        for name in (getattr(persona_spec, "mcp_servers", []) or [])
        if name in fleet_config.mcp_servers
    }
    return backend.create_session(
        persona_name=persona,
        cwd=cwd,
        mcp_servers=mcp_specs,
        model=persona_spec.model,
        mode=persona_spec.mode,
    )
