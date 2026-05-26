"""Unified persistent state for issue and PR loops.

Single file: ``.agent-fleet-state.json`` in the repo root. Top-level keys are
flat (no namespacing) since issue and PR state share no key names:

- issue keys: ``since``, ``seen``, ``in_flight``, ``queue``
- schedule keys: ``schedules`` (per-job ``next_due_at``, ``last_run_at``, ``in_flight``)
- PR keys: ``pr:<N>``, ``last_merge_ts``

On first load, legacy ``.agent-fleet-issue-state.json`` and
``.agent-fleet-loop-state.json`` are merged into the new file and renamed
``.bak``.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agent_fleet.state_store import JsonStateStore

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILENAME = ".agent-fleet-state.json"
LEGACY_ISSUE_FILENAME = ".agent-fleet-issue-state.json"
LEGACY_PR_FILENAME = ".agent-fleet-loop-state.json"


def state_path(repo_root: Path) -> Path:
    return repo_root / STATE_FILENAME


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migrate_legacy(repo_root: Path, target: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    legacy_paths = [repo_root / LEGACY_ISSUE_FILENAME, repo_root / LEGACY_PR_FILENAME]
    found = [p for p in legacy_paths if p.exists()]
    if not found:
        return merged
    for legacy in found:
        try:
            data = JsonStateStore(legacy).load()
        except Exception:
            logger.warning("Failed to read legacy state %s; skipping", legacy)
            continue
        if isinstance(data, dict):
            merged.update(data)
    JsonStateStore(target, atomic=True).save(merged)
    for legacy in found:
        bak = legacy.with_suffix(legacy.suffix + ".bak")
        try:
            legacy.rename(bak)
        except OSError:
            logger.warning("Failed to rename %s to %s", legacy, bak)
    logger.info("Migrated %d legacy state file(s) into %s", len(found), target)
    return merged


def load_state(path: Path) -> dict[str, Any]:
    """Load unified state, migrating from legacy files on first use."""
    if path.exists():
        return JsonStateStore(path, atomic=True).load()
    return _migrate_legacy(path.parent, path)


def save_state(path: Path, state: dict[str, Any]) -> None:
    JsonStateStore(path, atomic=True).save(state)


def apply_issue_defaults(
    state: dict[str, Any], since_override: str | None = None
) -> dict[str, Any]:
    state.setdefault("seen", [])
    state.setdefault("in_flight", {})
    if since_override:
        state["since"] = since_override
    else:
        state.setdefault("since", now_iso())
    return state


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
