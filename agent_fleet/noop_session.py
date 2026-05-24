"""Fallback session for backends without durable agent handles."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from agent_fleet.cursor_backend import CursorLLMResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agent_fleet.contracts.mcp import McpServerSpec


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
    ) -> CursorLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools
        return CursorLLMResult(
            stdout="",
            stderr="NoopSession: send() called on a backend without session support",
            exit_code=1,
            duration_s=0.0,
        )

    def dispose(self) -> None:
        self._disposed = True
