"""Tests for MCP catalog + per-persona allowlist parsing."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.mcp import HttpMcpServerSpec, StdioMcpServerSpec


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text(textwrap.dedent(body), encoding="utf-8")
    return cfg


def test_catalog_parses_stdio_and_http(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        mcp_servers:
          playwright:
            type: stdio
            command: npx
            args: ["-y", "@playwright/mcp@latest"]
          context7:
            type: http
            url: https://mcp.context7.com/mcp
            headers:
              Authorization: Bearer fixed-token
    """)
    fc = load_fleet_config(cfg)
    assert isinstance(fc.mcp_servers["playwright"], StdioMcpServerSpec)
    assert fc.mcp_servers["playwright"].command == "npx"
    assert isinstance(fc.mcp_servers["context7"], HttpMcpServerSpec)
    assert fc.mcp_servers["context7"].headers["Authorization"] == "Bearer fixed-token"


def test_env_var_expansion_in_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CONTEXT7_KEY", "super-secret")
    cfg = _write(tmp_path, """
        mcp_servers:
          context7:
            type: http
            url: https://mcp.context7.com/mcp
            headers:
              Authorization: Bearer ${CONTEXT7_KEY}
    """)
    fc = load_fleet_config(cfg)
    assert fc.mcp_servers["context7"].headers["Authorization"] == "Bearer super-secret"


def test_missing_env_var_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cfg = _write(tmp_path, """
        mcp_servers:
          context7:
            type: http
            url: https://mcp.context7.com/mcp
            headers:
              Authorization: Bearer ${MISSING_KEY}
    """)
    with pytest.raises(ValueError, match="MISSING_KEY"):
        load_fleet_config(cfg)


def test_persona_allowlist_resolves_against_catalog(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        mcp_servers:
          playwright:
            type: stdio
            command: npx
            args: ["-y", "@playwright/mcp"]
        personas:
          coder:
            prompt: coder.md
            mcp_servers: [playwright]
    """)
    fc = load_fleet_config(cfg)
    assert fc.personas["coder"].mcp_servers == ["playwright"]


def test_persona_allowlist_unknown_mcp_raises(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        mcp_servers:
          playwright:
            type: stdio
            command: npx
        personas:
          coder:
            prompt: coder.md
            mcp_servers: [does_not_exist]
    """)
    with pytest.raises(ValueError, match="does_not_exist"):
        load_fleet_config(cfg)


def test_no_mcp_section_yields_empty_catalog(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        personas:
          coder:
            prompt: coder.md
    """)
    fc = load_fleet_config(cfg)
    assert fc.mcp_servers == {}
    assert fc.personas["coder"].mcp_servers == []
