"""Shared visual-audit / Playwright dispatch classification."""

from __future__ import annotations


def is_visual_audit_dispatch(
    *,
    issue_labels: list[str] | None = None,
    title: str = "",
    body: str = "",
) -> bool:
    """Return True when a dispatch requires Playwright MCP."""
    if issue_labels and "visual-audit" in issue_labels:
        return True
    combined = f"{title}\n{body}".lower()
    return "playwright mcp" in combined or "[visual]" in title.lower()
