"""CLI environment checks shared across agent-fleet commands."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig


def require_backend_env(config: FleetConfig) -> int | None:
    """Return an exit code when required API keys are missing, else None."""
    backend_name = config.default_backend.lower()
    if backend_name == "cursor" and not os.environ.get("CURSOR_API_KEY"):
        print("error: CURSOR_API_KEY is not set", file=sys.stderr)
        return 1
    if backend_name == "kimi" and not os.environ.get("KIMI_API_KEY"):
        print(
            "error: KIMI_API_KEY is not set (Kimi Code subscription)",
            file=sys.stderr,
        )
        return 1
    return None
