"""Tests for MCP requirement contracts."""

from __future__ import annotations

from agent_fleet.contracts.mcp_requirement import McpRequirement, browser_prompt_block


def test_none_requirement_always_passes() -> None:
    result = McpRequirement.none().check(())
    assert result.passed
    assert result.reason == "not_required"


def test_playwright_visual_requires_navigate() -> None:
    req = McpRequirement.playwright_visual()
    assert not req.check(()).passed
    assert not req.check(("playwright.browser_snapshot",)).passed
    assert req.check(("playwright.browser_navigate",)).passed


def test_browser_prompt_block_contains_url() -> None:
    block = browser_prompt_block(base_url="https://example.test")
    assert "https://example.test" in block
    assert "browser_navigate" in block
