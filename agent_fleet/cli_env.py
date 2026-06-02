"""CLI environment checks shared across agent-fleet commands."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig


def require_backend_env(config: FleetConfig) -> int | None:
    """Return an exit code when required API keys are missing, else None."""
    from agent_fleet.backends import backend_env_var, backend_key_hint

    name = config.default_backend.lower()
    env = backend_env_var(name)
    if env and not os.environ.get(env):
        hint = backend_key_hint(name)
        suffix = f" ({hint})" if hint else ""
        print(f"error: {env} is not set{suffix}", file=sys.stderr)
        return 1
    return None
