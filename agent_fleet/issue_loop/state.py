"""Persistent watcher state for issue comment polling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_fleet.state_store import JsonStateStore

if TYPE_CHECKING:
    from pathlib import Path


def state_path(repo_root: Path, filename: str) -> Path:
    return repo_root / filename


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: Path, since_override: str | None = None) -> dict[str, Any]:
    store = JsonStateStore(path, atomic=True)
    state = store.load(
        {
            "seen": [],
            "in_flight": {},
            "since": since_override or now_iso(),
        }
    )
    state.setdefault("seen", [])
    state.setdefault("in_flight", {})
    state.setdefault("since", since_override or now_iso())
    if since_override:
        state["since"] = since_override
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    JsonStateStore(path, atomic=True).save(state)
