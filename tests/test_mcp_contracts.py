"""Tests for MCP server config dataclasses."""

from __future__ import annotations

import pytest

from agent_fleet.contracts.mcp import (
    HttpMcpServerSpec,
    McpServerSpec,
    StdioMcpServerSpec,
    parse_mcp_server_spec,
)


def test_stdio_spec_minimum_fields() -> None:
    spec = StdioMcpServerSpec(command="npx", args=("-y", "@playwright/mcp"))
    assert spec.command == "npx"
    assert spec.args == ("-y", "@playwright/mcp")
    assert spec.env == {}


def test_http_spec_with_headers() -> None:
    spec = HttpMcpServerSpec(
        url="https://mcp.context7.com/mcp",
        headers={"Authorization": "Bearer x"},
    )
    assert spec.url == "https://mcp.context7.com/mcp"
    assert spec.headers["Authorization"] == "Bearer x"


def test_parse_stdio_from_dict() -> None:
    raw = {"type": "stdio", "command": "uvx", "args": ["serena-mcp-server"]}
    spec = parse_mcp_server_spec("serena", raw)
    assert isinstance(spec, StdioMcpServerSpec)
    assert spec.command == "uvx"


def test_parse_http_from_dict() -> None:
    raw = {"type": "http", "url": "https://example.com/mcp"}
    spec = parse_mcp_server_spec("docs", raw)
    assert isinstance(spec, HttpMcpServerSpec)
    assert spec.url == "https://example.com/mcp"


def test_parse_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown MCP type"):
        parse_mcp_server_spec("bad", {"type": "websocket", "url": "x"})


def test_parse_requires_url_for_http() -> None:
    with pytest.raises(ValueError, match="url is required"):
        parse_mcp_server_spec("bad", {"type": "http"})


def test_parse_requires_command_for_stdio() -> None:
    with pytest.raises(ValueError, match="command is required"):
        parse_mcp_server_spec("bad", {"type": "stdio"})


def test_mcp_server_spec_is_union() -> None:
    # McpServerSpec is the union type used by callers.
    stdio: McpServerSpec = StdioMcpServerSpec(command="x")
    http: McpServerSpec = HttpMcpServerSpec(url="y")
    assert stdio.command == "x"
    assert http.url == "y"
