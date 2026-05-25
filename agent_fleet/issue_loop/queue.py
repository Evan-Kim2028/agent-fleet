"""FIFO issue dispatch queue (.agent-fleet-queue.yaml)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import yaml

from agent_fleet.capacity import RETRYABLE_ADMISSION_REASONS
from agent_fleet.in_flight import reap_in_flight
from agent_fleet.issue_loop import github_ops

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.capacity.gate import FleetCapacityGate
    from agent_fleet.issue_loop.config import IssueDispatchConfig, IssueQueueConfig

logger = logging.getLogger(__name__)

QueueAdvance = Literal["dispatch", "complete"]
SpawnDispatchFn = Callable[..., int | None]


@dataclass(frozen=True)
class QueueItem:
    issue: int
    persona: str
    note: str = ""


def queue_path(repo_root: Path, config: IssueQueueConfig) -> Path:
    return (repo_root / config.file).resolve()


def queue_content_fingerprint(items: list[QueueItem]) -> str:
    return "|".join(f"{item.issue}:{item.persona}" for item in items)


def sync_queue_fingerprint(state: dict[str, Any], items: list[QueueItem]) -> None:
    """Reset head only when ordered queue items change (not on note-only edits)."""
    qs = queue_state(state)
    fingerprint = queue_content_fingerprint(items)
    if qs.get("fingerprint") != fingerprint:
        qs["fingerprint"] = fingerprint
        qs["head"] = 0
        qs.pop("waiting_index", None)


def load_queue_items(repo_root: Path, config: IssueQueueConfig) -> list[QueueItem]:
    path = queue_path(repo_root, config)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    entries = raw.get("queue") or []
    if not isinstance(entries, list):
        return []

    items: list[QueueItem] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        issue_raw = entry.get("issue")
        persona = str(entry.get("persona") or "").strip()
        if issue_raw is None or not persona:
            continue
        try:
            issue = int(issue_raw)
        except (TypeError, ValueError):
            continue
        note = str(entry.get("note") or "").strip()
        items.append(QueueItem(issue=issue, persona=persona, note=note))
    return items


def queue_state(state: dict[str, Any]) -> dict[str, Any]:
    qs = state.setdefault("queue", {})
    qs.setdefault("head", 0)
    qs.setdefault("fingerprint", "")
    return qs


def build_queue_comment(item: QueueItem, dispatch_config: IssueDispatchConfig) -> str:
    lines = [f"/agent --persona {item.persona}"]
    if item.note:
        lines.extend(["", item.note])
    lines.extend(["", f"Queue dispatch (FIFO). {dispatch_config.comment_marker}"])
    return "\n".join(lines)


def queue_status(
    repo_root: Path,
    config: IssueQueueConfig,
    state: dict[str, Any],
) -> dict[str, Any]:
    path = queue_path(repo_root, config)
    items = load_queue_items(repo_root, config)
    qs = queue_state(state)
    head = int(qs.get("head", 0))
    in_flight = state.get("in_flight") or {}
    pending = [
        {"index": idx, "issue": item.issue, "persona": item.persona}
        for idx, item in enumerate(items)
        if idx >= head
    ]
    return {
        "enabled": True,
        "file": str(path),
        "advance": config.advance,
        "head": head,
        "total": len(items),
        "waiting_index": qs.get("waiting_index"),
        "pending": pending[:20],
        "in_flight_issues": sorted(in_flight.keys(), key=int),
    }


def _issue_in_flight(state: dict[str, Any], issue_number: int) -> bool:
    runs = (state.get("in_flight") or {}).get(str(issue_number)) or []
    return bool(runs)


def _issue_is_open(issue_number: int, *, cwd: Path) -> bool:
    try:
        result = github_ops.issue_view(issue_number, cwd=cwd)
    except Exception as exc:
        logger.warning("Queue: could not load issue #%s: %s", issue_number, exc)
        return False
    state = str(result.get("state") or "").upper()
    return state == "OPEN"


def poll_queue(
    *,
    repo_root: Path,
    dispatch_config: IssueDispatchConfig,
    queue_config: IssueQueueConfig,
    state: dict[str, Any],
    capacity_gate: FleetCapacityGate,
    spawn_dispatch: SpawnDispatchFn,
    available_ram_gb: float,
) -> tuple[list[dict[str, str]], bool]:
    """Drain the FIFO queue. Returns (results, retryable_deferred)."""
    reap_in_flight(state)
    items = load_queue_items(repo_root, queue_config)
    if not items:
        return [], False

    sync_queue_fingerprint(state, items)
    open_fleet_issues = github_ops.open_fleet_pr_issue_numbers(cwd=repo_root)
    qs = queue_state(state)
    results: list[dict[str, str]] = []
    retryable_deferred = False

    if queue_config.advance == "complete":
        waiting_index = qs.get("waiting_index")
        if waiting_index is not None:
            idx = int(waiting_index)
            if 0 <= idx < len(items):
                waiting_issue = items[idx].issue
                if _issue_in_flight(state, waiting_issue):
                    return results, False
                qs["head"] = idx + 1
            qs.pop("waiting_index", None)

    head = int(qs.get("head", 0))
    while head < len(items):
        item = items[head]

        if not _issue_is_open(item.issue, cwd=repo_root):
            logger.info("Queue skip closed issue #%s at index %s", item.issue, head)
            qs["head"] = head + 1
            head += 1
            results.append({"issue": str(item.issue), "status": "queue_skipped_closed"})
            continue

        if item.issue in open_fleet_issues:
            logger.info(
                "Queue skip issue #%s at index %s: open fleet PR already exists",
                item.issue,
                head,
            )
            qs["head"] = head + 1
            head += 1
            results.append({"issue": str(item.issue), "status": "queue_skipped_has_pr"})
            continue

        if _issue_in_flight(state, item.issue):
            if queue_config.advance == "complete":
                qs["waiting_index"] = head
                logger.info(
                    "Queue waiting for issue #%s to finish (index %s, mode=complete)",
                    item.issue,
                    head,
                )
                break
            logger.info(
                "Queue advance past in-flight issue #%s at index %s (mode=dispatch)",
                item.issue,
                head,
            )
            qs["head"] = head + 1
            head += 1
            results.append(
                {"issue": str(item.issue), "status": "queue_already_in_flight"}
            )
            continue

        issue_labels: list[str] = []
        issue_title = ""
        issue_body = ""
        try:
            issue = github_ops.issue_view(item.issue, cwd=repo_root)
            issue_title = str(issue.get("title") or "")
            issue_body = str(issue.get("body") or "")
            issue_labels = github_ops.issue_labels(item.issue, cwd=repo_root)
        except Exception as exc:
            logger.warning(
                "Queue: could not load issue #%s metadata: %s", item.issue, exc
            )

        from agent_fleet.capacity import is_visual_audit_dispatch

        is_visual_audit = is_visual_audit_dispatch(
            issue_labels=issue_labels,
            title=issue_title,
            body=issue_body,
        )
        admission = capacity_gate.try_admit(
            state,
            issue_number=item.issue,
            persona=item.persona,
            is_visual_audit=is_visual_audit,
            available_ram_gb=available_ram_gb,
        )
        if not admission.allowed:
            retryable = admission.reason in RETRYABLE_ADMISSION_REASONS
            if retryable:
                retryable_deferred = True
                logger.info(
                    "Queue deferred at head #%s (index %s): %s",
                    item.issue,
                    head,
                    admission.reason,
                )
            else:
                logger.info(
                    "Queue skip issue #%s at index %s: %s",
                    item.issue,
                    head,
                    admission.reason,
                )
                qs["head"] = head + 1
                head += 1
                results.append(
                    {
                        "issue": str(item.issue),
                        "status": f"queue_skipped_{admission.reason}",
                    }
                )
            break

        comment_body = build_queue_comment(item, dispatch_config)
        logger.info(
            "Queue dispatch: issue #%s persona=%s index=%s advance=%s",
            item.issue,
            item.persona,
            head,
            queue_config.advance,
        )
        pid = spawn_dispatch(
            issue_number=item.issue,
            comment_body=comment_body,
            persona=item.persona,
            repo_root=repo_root,
        )
        if pid is None:
            break

        in_flight = state.setdefault("in_flight", {}).setdefault(str(item.issue), [])
        in_flight.append(
            {
                "pid": pid,
                "persona": item.persona,
                "visual_audit": is_visual_audit,
                "from_queue": True,
                "queue_index": head,
            }
        )
        results.append(
            {
                "issue": str(item.issue),
                "status": "queue_dispatched",
                "pid": str(pid),
                "queue_index": str(head),
            }
        )

        if queue_config.advance == "complete":
            qs["waiting_index"] = head
            break

        qs["head"] = head + 1
        head += 1

    return results, retryable_deferred