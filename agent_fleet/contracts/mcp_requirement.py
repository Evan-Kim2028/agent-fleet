"""MCP usage requirements for fleet phases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpCheckResult:
    passed: bool
    reason: str
    missing_tools: tuple[str, ...] = ()
    observed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpRequirement:
    """Describe and verify MCP tool usage for a phase."""

    required: bool = False
    servers: tuple[str, ...] = ()
    required_tool_suffixes: tuple[str, ...] = ()

    @classmethod
    def none(cls) -> McpRequirement:
        return cls(required=False)

    @classmethod
    def playwright_visual(
        cls,
        *,
        servers: tuple[str, ...] = ("playwright",),
    ) -> McpRequirement:
        return cls(
            required=True,
            servers=servers,
            required_tool_suffixes=("browser_navigate",),
        )

    def expect_tools(self) -> bool:
        return self.required

    def check(self, mcp_tool_calls: tuple[str, ...]) -> McpCheckResult:
        if not self.required:
            return McpCheckResult(True, "not_required", observed_tools=mcp_tool_calls)

        if not mcp_tool_calls:
            return McpCheckResult(
                False,
                "no_mcp_tools_invoked",
                observed_tools=mcp_tool_calls,
            )

        if not self.required_tool_suffixes:
            return McpCheckResult(True, "ok", observed_tools=mcp_tool_calls)

        missing: list[str] = []
        for suffix in self.required_tool_suffixes:
            suffixes = (f".{suffix}", suffix)
            if not any(label.endswith(suffixes) for label in mcp_tool_calls):
                missing.append(suffix)
        if missing:
            return McpCheckResult(
                False,
                "missing_required_tools",
                missing_tools=tuple(missing),
                observed_tools=mcp_tool_calls,
            )
        return McpCheckResult(True, "ok", observed_tools=mcp_tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "servers": list(self.servers),
            "required_tool_suffixes": list(self.required_tool_suffixes),
        }


def browser_prompt_block(*, base_url: str = "https://silphcoanalytics.xyz") -> str:
    """Shared Playwright MCP instructions for research/implement prompts."""
    return (
        "\n\n## Playwright MCP (required)\n"
        f"You MUST use Playwright MCP tools on {base_url} before and after code changes. "
        "Call at least browser_navigate and browser_snapshot (or browser_take_screenshot). "
        "Include what you observed in the browser in your findings or verification notes. "
        "Do not skip browser verification."
    )
