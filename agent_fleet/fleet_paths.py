"""Canonical filesystem locations for agent-fleet."""

from __future__ import annotations

import os
from pathlib import Path


def agent_fleet_home() -> Path:
    """User-local agent-fleet root (config, runs, level-up, skills)."""
    raw = os.environ.get("AGENT_FLEET_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".agent-fleet").resolve()


def default_fleet_config_path() -> Path:
    """Global fleet.yaml location."""
    for env_name in ("AGENT_FLEET_CONFIG", "CODING_FLEET_CONFIG"):
        raw = os.environ.get(env_name)
        if raw:
            return Path(raw).expanduser().resolve()
    return agent_fleet_home() / "fleet.yaml"


def default_runs_dir() -> Path:
    """JSONL run log directory."""
    raw = os.environ.get("AGENT_FLEET_RUNS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return agent_fleet_home() / "fleet" / "runs"


def user_skill_dir() -> Path:
    """Optional user-installed skills for fleet personas."""
    return agent_fleet_home() / "skills"


def ensure_agent_fleet_home() -> Path:
    """Create ~/.agent-fleet layout if missing."""
    home = agent_fleet_home()
    for sub in ("fleet/runs", "level_up", "journal", "skills", "pr-review-logs", "scripts"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home
