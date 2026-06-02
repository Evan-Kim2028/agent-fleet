"""Backend factory — registry-driven; Cursor SDK (default) and Kimi Code CLI built-in."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agent_fleet.agent_mode import coerce_agent_mode
from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.kimi_backend import KimiBackend

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import LLMBackend

# name → factory(config) callable; mutate to register new backends at import time.
_REGISTRY: dict[str, Callable[[FleetConfig], LLMBackend]] = {}


def register(name: str, factory: Callable[[FleetConfig], LLMBackend]) -> None:
    """Register a backend factory under *name* (lower-cased)."""
    _REGISTRY[name.lower()] = factory


def _make_cursor(config: FleetConfig) -> LLMBackend:
    if not os.environ.get("CURSOR_API_KEY"):
        pass  # CursorBackend returns a clear error at run time
    return CursorBackend(
        default_model=config.default_model,
        default_mode=coerce_agent_mode(config.default_mode),
    )


def _make_kimi(config: FleetConfig) -> LLMBackend:
    model = config.default_model
    if model == "composer-2.5":
        model = "kimi-for-coding"
    return KimiBackend(
        model=model,
        kimi_bin=getattr(config, "kimi_bin", None),
    )


register("cursor", _make_cursor)
register("kimi", _make_kimi)


def make_backend(config: FleetConfig) -> LLMBackend:
    """Return the configured LLM backend."""
    name = (getattr(config, "default_backend", None) or "cursor").lower()
    factory = _REGISTRY.get(name)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown default_backend {name!r}. Known backends: {known}.")
    return factory(config)
