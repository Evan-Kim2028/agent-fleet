"""CLI environment checks shared across agent-fleet commands."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig


def require_backend_env(config: FleetConfig) -> int | None:
    """Return an exit code when required auth is missing, else None."""
    from agent_fleet.backends import backend_auth_probe, backend_env_var, backend_key_hint

    name = config.default_backend.lower()
    probe = backend_auth_probe(name)
    if probe is not None:
        ok, detail, fix = probe()
        if not ok:
            msg = detail if not fix else f"{detail}; {fix}"
            print(f"error: {msg}", file=sys.stderr)
            return 1
        return None

    env = backend_env_var(name)
    if env and not os.environ.get(env):
        hint = backend_key_hint(name)
        suffix = f" ({hint})" if hint else ""
        print(f"error: {env} is not set{suffix}", file=sys.stderr)
        return 1
    return None
