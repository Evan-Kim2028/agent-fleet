"""Dataclasses describing MCP server configurations forwarded to session-capable backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class StdioMcpServerSpec:
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True)
class HttpMcpServerSpec:
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    auth_client_id: str | None = None
    auth_client_secret: str | None = None
    auth_scopes: tuple[str, ...] = ()


McpServerSpec = StdioMcpServerSpec | HttpMcpServerSpec


def parse_mcp_server_spec(name: str, raw: Mapping[str, Any]) -> McpServerSpec:
    kind = str(raw.get("type") or "stdio").lower()
    if kind == "stdio":
        command = raw.get("command")
        if not command:
            raise ValueError(f"MCP {name!r}: command is required for stdio")
        return StdioMcpServerSpec(
            command=str(command),
            args=tuple(str(a) for a in raw.get("args") or ()),
            env=dict(raw.get("env") or {}),
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        )
    if kind in {"http", "sse"}:
        url = raw.get("url")
        if not url:
            raise ValueError(f"MCP {name!r}: url is required for {kind}")
        auth = raw.get("auth") or {}
        return HttpMcpServerSpec(
            url=str(url),
            headers=dict(raw.get("headers") or {}),
            auth_client_id=auth.get("client_id") or auth.get("CLIENT_ID"),
            auth_client_secret=auth.get("client_secret") or auth.get("CLIENT_SECRET"),
            auth_scopes=tuple(str(s) for s in (auth.get("scopes") or ())),
        )
    raise ValueError(f"MCP {name!r}: unknown MCP type {kind!r}")
