"""Workstream configuration from .agent-fleet.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkstreamItem:
    id: str
    persona: str
    goal: str
    target_branch: str | None = None
    base_branch: str | None = None
    pipeline: str | None = None
    context: str = ""


@dataclass(frozen=True)
class WorkstreamsConfig:
    """Named batch of fleet tasks declared in repo config."""

    plan: str | None = None
    base_branch: str | None = None
    default_target_branch: str | None = None
    pipeline: str = "code_review"
    sequential_stack: bool = True
    items: tuple[WorkstreamItem, ...] = ()

    def get(self, item_id: str) -> WorkstreamItem | None:
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def ids(self) -> list[str]:
        return [item.id for item in self.items]


def load_workstreams_config(raw: dict[str, Any] | None) -> WorkstreamsConfig | None:
    section = (raw or {}).get("workstreams")
    if not section or section is False:
        return None
    if not isinstance(section, dict):
        return None

    items_raw = section.get("items") or section.get("workstreams") or []
    items: list[WorkstreamItem] = []
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id") or "").strip()
        persona = str(entry.get("persona") or "").strip()
        goal = str(entry.get("goal") or "").strip()
        if not item_id or not persona or not goal:
            continue
        items.append(
            WorkstreamItem(
                id=item_id,
                persona=persona,
                goal=goal,
                target_branch=_optional_str(entry.get("target_branch")),
                base_branch=_optional_str(entry.get("base_branch")),
                pipeline=_optional_str(entry.get("pipeline")),
                context=str(entry.get("context") or ""),
            )
        )

    if not items:
        return None

    return WorkstreamsConfig(
        plan=_optional_str(section.get("plan")),
        base_branch=_optional_str(section.get("base_branch")),
        default_target_branch=_optional_str(section.get("default_target_branch")),
        pipeline=str(section.get("pipeline") or "code_review"),
        sequential_stack=bool(section.get("sequential_stack", True)),
        items=tuple(items),
    )


def _optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
