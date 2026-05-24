"""Issue dispatch configuration from .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class IssueDispatchConfig:
    """Settings for /agent --persona issue comment dispatch."""

    enabled: bool = False
    poll_interval_s: int = 30
    trigger_pattern: str = r"/agent\s+--persona\s+(\S+)"
    stop_pattern: str = r"/agent\s+stop\b"
    state_file: str = ".agent-fleet-issue-state.json"
    mutex_label_prefix: str = "agent-running"
    running_label_prefix: str = "fleet-running"
    max_in_flight_per_issue: int = 3
    max_in_flight_visual_audit: int = 1
    max_concurrent_dispatches: int = 4
    max_concurrent_visual_audit: int = 2
    min_available_ram_gb: float = 8.0
    visual_audit_ram_gb: float = 6.0
    comment_marker: str = "<!-- agent-fleet-watcher -->"


def load_issue_dispatch_config(
    _repo_root: Path,
    raw: dict[str, Any] | None,
) -> IssueDispatchConfig | None:
    section = (raw or {}).get("issue_dispatch")
    if not section:
        return None
    if not isinstance(section, dict):
        return None
    return IssueDispatchConfig(
        enabled=bool(section.get("enabled", False)),
        poll_interval_s=int(section.get("poll_interval_s", 30)),
        trigger_pattern=str(section.get("trigger_pattern", r"/agent\s+--persona\s+(\S+)")),
        stop_pattern=str(section.get("stop_pattern", r"/agent\s+stop\b")),
        state_file=str(section.get("state_file", ".agent-fleet-issue-state.json")),
        mutex_label_prefix=str(section.get("mutex_label_prefix", "agent-running")),
        running_label_prefix=str(section.get("running_label_prefix", "fleet-running")),
        max_in_flight_per_issue=int(section.get("max_in_flight_per_issue", 3)),
        max_in_flight_visual_audit=int(section.get("max_in_flight_visual_audit", 1)),
        max_concurrent_dispatches=int(section.get("max_concurrent_dispatches", 4)),
        max_concurrent_visual_audit=int(section.get("max_concurrent_visual_audit", 2)),
        min_available_ram_gb=float(section.get("min_available_ram_gb", 8.0)),
        visual_audit_ram_gb=float(section.get("visual_audit_ram_gb", 6.0)),
        comment_marker=str(section.get("comment_marker", "<!-- agent-fleet-watcher -->")),
    )
