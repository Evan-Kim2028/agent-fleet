"""Fallback session for backends without durable agent handles.

Backend-agnostic: owns its own ``NoopLLMResult`` satisfying the ``LLMResult``
protocol so a non-session backend (kimi, openrouter, …) can use NoopSession
without importing any concrete backend's result type.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agent_fleet.contracts.mcp import McpServerSpec


@dataclass(frozen=True)
class NoopLLMResult:
    """Protocol-compliant LLMResult for the no-op fallback path.

    Satisfies ``agent_fleet.hooks.LLMResult`` without coupling to any concrete
    backend's result type. Also serves as the canonical stub for tests that
    need a result without exercising a real backend.
    """

    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None
    usage: dict[str, int] | None = None


class NoopSession:
    """Records MCP config and returns a clear error on send()."""

    agent_id: str | None = None

    def __init__(
        self,
        *,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,
        persona_name: str | None = None,
    ) -> None:
        self._mcp_servers = dict(mcp_servers or {})
        self._persona = persona_name
        self._disposed = False
        if self._mcp_servers:
            print(
                f"NoopSession: persona={persona_name!r} configured "
                f"{len(self._mcp_servers)} MCP server(s) but this backend "
                f"does not support MCPs; they will be ignored.",
                file=sys.stderr,
            )

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
    ) -> NoopLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools
        return NoopLLMResult(
            stdout="",
            stderr="NoopSession: send() called on a backend without session support",
            exit_code=1,
            duration_s=0.0,
        )

    def dispose(self) -> None:
        self._disposed = True
