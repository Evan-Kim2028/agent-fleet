"""Persistent state for PR loop watcher."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def state_path(repo_root: Path, filename: str) -> Path:
    return repo_root / filename


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError, OSError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def merge_cooldown_remaining(state: dict[str, Any], cooldown_s: int) -> float:
    if cooldown_s <= 0:
        return 0.0
    last_ts = float(state.get("last_merge_ts") or 0)
    if last_ts <= 0:
        return 0.0
    elapsed = time.time() - last_ts
    return max(0.0, cooldown_s - elapsed)


def pr_state_key(pr_number: int) -> str:
    return f"pr:{pr_number}"


def get_pr_state(state: dict[str, Any], pr_number: int) -> dict[str, Any]:
    entry = state.get(pr_state_key(pr_number))
    return entry if isinstance(entry, dict) else {}


def set_pr_state(state: dict[str, Any], pr_number: int, entry: dict[str, Any]) -> None:
    state[pr_state_key(pr_number)] = entry
