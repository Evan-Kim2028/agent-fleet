"""Persistent watcher state for issue comment polling."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def state_path(repo_root: Path, filename: str) -> Path:
    return repo_root / filename


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: Path, since_override: str | None = None) -> dict[str, Any]:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            state = {}
    else:
        state = {}
    state.setdefault("seen", [])
    state.setdefault("in_flight", {})
    state.setdefault("since", since_override or now_iso())
    if since_override:
        state["since"] = since_override
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)
