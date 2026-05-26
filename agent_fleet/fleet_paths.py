"""Canonical filesystem locations for agent-fleet (independent of Hermes)."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_LEGACY_HERMES_CONFIG = Path.home() / ".hermes" / "coding_fleet" / "fleet.yaml"
_LEGACY_HERMES_RUNS = Path.home() / ".hermes" / "fleet" / "runs"


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
    preferred = agent_fleet_home() / "fleet.yaml"
    if preferred.is_file():
        return preferred
    if _LEGACY_HERMES_CONFIG.is_file():
        logger.warning(
            "Using deprecated fleet config %s — run `agent-fleet migrate-home` "
            "to move settings to %s",
            _LEGACY_HERMES_CONFIG,
            preferred,
        )
        return _LEGACY_HERMES_CONFIG
    return preferred


def default_runs_dir() -> Path:
    """JSONL run log directory."""
    raw = os.environ.get("AGENT_FLEET_RUNS_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    preferred = agent_fleet_home() / "runs"
    if preferred.is_dir() or not _LEGACY_HERMES_RUNS.is_dir():
        return preferred
    logger.warning(
        "Using deprecated runs dir %s — run `agent-fleet migrate-home` to move logs to %s",
        _LEGACY_HERMES_RUNS,
        preferred,
    )
    return _LEGACY_HERMES_RUNS


def user_skill_dir() -> Path:
    """Optional user-installed skills for fleet personas."""
    return agent_fleet_home() / "skills"


def ensure_agent_fleet_home() -> Path:
    """Create ~/.agent-fleet layout if missing."""
    home = agent_fleet_home()
    for sub in ("runs", "level_up", "journal", "skills", "pr-review", "scripts"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def migrate_from_hermes(*, dry_run: bool = False) -> dict[str, str]:
    """Copy legacy Hermes-hosted fleet config/logs into ~/.agent-fleet/."""
    home = ensure_agent_fleet_home()
    actions: dict[str, str] = {}

    target_config = home / "fleet.yaml"
    if _LEGACY_HERMES_CONFIG.is_file() and not target_config.is_file():
        if dry_run:
            actions["fleet.yaml"] = f"would copy {_LEGACY_HERMES_CONFIG} -> {target_config}"
        else:
            shutil.copy2(_LEGACY_HERMES_CONFIG, target_config)
            actions["fleet.yaml"] = f"copied to {target_config}"

    target_runs = home / "runs"
    if _LEGACY_HERMES_RUNS.is_dir():
        target_runs.mkdir(parents=True, exist_ok=True)
        copied = 0
        for path in _LEGACY_HERMES_RUNS.glob("*.jsonl"):
            dest = target_runs / path.name
            if dest.exists():
                continue
            if dry_run:
                copied += 1
                continue
            shutil.copy2(path, dest)
            copied += 1
        if copied:
            key = "runs (dry-run)" if dry_run else "runs"
            actions[key] = f"{copied} jsonl file(s) -> {target_runs}"

    legacy_readme = Path.home() / ".hermes" / "coding_fleet" / "README.txt"
    if _LEGACY_HERMES_CONFIG.is_file() and not dry_run:
        legacy_readme.parent.mkdir(parents=True, exist_ok=True)
        legacy_readme.write_text(
            "Fleet config moved to ~/.agent-fleet/fleet.yaml\n"
            "Run logs: ~/.agent-fleet/runs/\n"
            "Scripts: ~/.agent-fleet/scripts/\n"
            "Hermes uses agent-fleet via the cursor-fleet plugin only.\n",
            encoding="utf-8",
        )
        actions["hermes_notice"] = f"wrote {legacy_readme}"

    return actions
