"""Shim — real module lives in agent_fleet/agent_fleet/integrations/."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "agent_fleet" / "integrations" / "local_git.py"
_SPEC = importlib.util.spec_from_file_location("_agent_fleet_local_git", _PATH)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

LocalGitOps = _MOD.LocalGitOps
git_ops_from_repo = _MOD.git_ops_from_repo

__all__ = ["LocalGitOps", "git_ops_from_repo"]
