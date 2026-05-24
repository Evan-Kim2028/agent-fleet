"""Per-task agent session abstraction wrapping the Cursor SDK's durable agent."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agent_fleet.cursor_backend import CursorLLMResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agent_fleet.contracts.mcp import McpServerSpec


@runtime_checkable
class AgentSession(Protocol):
    """A long-lived agent handle scoped to a single task. Multiple phases
    issue successive `send()` calls into the same conversation."""

    agent_id: str | None

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
    ) -> CursorLLMResult: ...

    def dispose(self) -> None: ...


class NoopSession:
    """Fallback session for backends that do not support persistent agents.
    Records that it was used and returns a clear error on send()."""

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
