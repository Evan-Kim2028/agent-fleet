"""Filesystem paths for persona level-up storage."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.fleet_paths import agent_fleet_home

LEVEL_UP_ROOT = agent_fleet_home() / "level_up"
JOURNAL_INDEX_PATH = agent_fleet_home() / "journal" / "index.jsonl"
FLEET_TIER = "_fleet"

COMPACTION_IDLE_DAYS = 7
WEIGHT_PR_LOOP_ROUND2 = 2.0
WEIGHT_REVIEW_FIX_SUCCESS = 1.5
WEIGHT_DEFAULT = 1.0


def repo_key(name: str | None = None, repo_root: Path | str | None = None) -> str:
    """Return a stable directory key for a repo."""
    if name and str(name).strip():
        return str(name).strip()
    if repo_root is not None:
        return Path(repo_root).name
    return "_unknown"


def persona_dir(repo_key_value: str, persona: str) -> Path:
    """Directory for repo-scoped persona level-up data."""
    return LEVEL_UP_ROOT / repo_key_value / persona


def fleet_persona_dir(persona: str) -> Path:
    """Directory for fleet-tier (_fleet) persona level-up data."""
    return LEVEL_UP_ROOT / FLEET_TIER / persona
