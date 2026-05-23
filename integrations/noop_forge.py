"""Shim — real module lives in agent_fleet/agent_fleet/integrations/."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "agent_fleet" / "integrations" / "noop_forge.py"
_SPEC = importlib.util.spec_from_file_location("_agent_fleet_noop_forge", _PATH)
assert _SPEC and _SPEC.loader
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

NoOpForge = _MOD.NoOpForge

__all__ = ["NoOpForge"]
