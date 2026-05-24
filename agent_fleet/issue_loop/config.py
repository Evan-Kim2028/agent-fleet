"""Issue dispatch configuration from .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_fleet.capacity.config import warn_deprecated_issue_dispatch_capacity

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class IssueDispatchConfig:
    """Settings for /agent --persona issue comment dispatch.

    Concurrency and RAM limits live in the top-level ``capacity`` block
    (see ``agent_fleet.capacity``).
    """

    enabled: bool = False
    poll_interval_s: int = 30
    trigger_pattern: str = r"/agent\s+--persona\s+(\S+)"
    stop_pattern: str = r"/agent\s+stop\b"
    state_file: str = ".agent-fleet-issue-state.json"
    mutex_label_prefix: str = "agent-running"
    running_label_prefix: str = "fleet-running"
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
    warn_deprecated_issue_dispatch_capacity(section)
    return IssueDispatchConfig(
        enabled=bool(section.get("enabled", False)),
        poll_interval_s=int(section.get("poll_interval_s", 30)),
        trigger_pattern=str(section.get("trigger_pattern", r"/agent\s+--persona\s+(\S+)")),
        stop_pattern=str(section.get("stop_pattern", r"/agent\s+stop\b")),
        state_file=str(section.get("state_file", ".agent-fleet-issue-state.json")),
        mutex_label_prefix=str(section.get("mutex_label_prefix", "agent-running")),
        running_label_prefix=str(section.get("running_label_prefix", "fleet-running")),
        comment_marker=str(section.get("comment_marker", "<!-- agent-fleet-watcher -->")),
    )
