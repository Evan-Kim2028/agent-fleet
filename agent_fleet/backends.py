"""Backend factory — Cursor SDK (default) or Kimi Code CLI (optional)."""

from __future__ import annotations

import os

from agent_fleet.config import FleetConfig
from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.hooks import LLMBackend
from agent_fleet.kimi_backend import KimiBackend


def make_backend(config: FleetConfig) -> LLMBackend:
    """Return the configured LLM backend."""
    name = (getattr(config, "default_backend", None) or "cursor").lower()
    if name == "kimi":
        model = config.default_model
        if model == "composer-2.5":
            model = "kimi-for-coding"
        return KimiBackend(
            model=model,
            kimi_bin=getattr(config, "kimi_bin", None),
        )
    if name != "cursor":
        raise ValueError(f"Unknown default_backend {name!r}. Use 'cursor' or 'kimi'.")
    if not os.environ.get("CURSOR_API_KEY"):
        pass  # CursorBackend returns a clear error at run time
    return CursorBackend(default_model=config.default_model, default_mode=config.default_mode)
