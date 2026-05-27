"""Issue dispatch configuration from .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agent_fleet.capacity.config import warn_deprecated_issue_dispatch_capacity

if TYPE_CHECKING:
    from pathlib import Path

QueueAdvance = Literal["dispatch", "complete"]


@dataclass
class IssueQueueConfig:
    """FIFO queue backed by a repo-root YAML file."""

    enabled: bool = False
    file: str = ".agent-fleet-queue.yaml"
    advance: QueueAdvance = "dispatch"


@dataclass
class BacklogDispatcherConfig:
    """Settings for the backlog auto-dispatcher.

    When enabled, the dispatcher polls GitHub for issues labelled ``label``
    and posts ``/agent --persona <X>`` comments to eligible issues so that the
    existing comment-trigger path handles the actual dispatch.

    Default ``enabled=False`` so merging this is safe without opting in.
    """

    enabled: bool = False
    label: str = "fleet-ready"
    persona_label_prefix: str = "fleet-persona/"
    default_persona: str = "data"
    tick_interval_s: int = 600
    marker_freshness_s: int = 300


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
    mutex_label_prefix: str = "agent-running"
    running_label_prefix: str = "fleet-running"
    comment_marker: str = "<!-- agent-fleet-watcher -->"
    queue: IssueQueueConfig | None = None


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
    queue_raw = section.get("queue")
    queue_cfg: IssueQueueConfig | None = None
    if isinstance(queue_raw, dict) and queue_raw.get("enabled"):
        advance_raw = str(queue_raw.get("advance", "dispatch"))
        advance: QueueAdvance = "complete" if advance_raw == "complete" else "dispatch"
        queue_cfg = IssueQueueConfig(
            enabled=True,
            file=str(queue_raw.get("file", ".agent-fleet-queue.yaml")),
            advance=advance,
        )
    return IssueDispatchConfig(
        enabled=bool(section.get("enabled", False)),
        poll_interval_s=int(section.get("poll_interval_s", 30)),
        trigger_pattern=str(section.get("trigger_pattern", r"/agent\s+--persona\s+(\S+)")),
        stop_pattern=str(section.get("stop_pattern", r"/agent\s+stop\b")),
        mutex_label_prefix=str(section.get("mutex_label_prefix", "agent-running")),
        running_label_prefix=str(section.get("running_label_prefix", "fleet-running")),
        comment_marker=str(section.get("comment_marker", "<!-- agent-fleet-watcher -->")),
        queue=queue_cfg,
    )


def load_backlog_dispatcher_config(
    raw: dict[str, Any] | None,
) -> BacklogDispatcherConfig:
    """Parse the ``backlog_dispatcher`` section from repo YAML.

    Returns a disabled config when the section is absent or malformed, so
    this is always safe to call.
    """
    section = (raw or {}).get("backlog_dispatcher")
    if not section or not isinstance(section, dict):
        return BacklogDispatcherConfig()
    return BacklogDispatcherConfig(
        enabled=bool(section.get("enabled", False)),
        label=str(section.get("label", "fleet-ready")),
        persona_label_prefix=str(section.get("persona_label_prefix", "fleet-persona/")),
        default_persona=str(section.get("default_persona", "data")),
        tick_interval_s=int(section.get("tick_interval_s", 600)),
        marker_freshness_s=int(section.get("marker_freshness_s", 300)),
    )
