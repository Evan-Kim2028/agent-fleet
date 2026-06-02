"""Backend factory — registry-driven; Cursor SDK (default) and Kimi Code CLI built-in."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_fleet.agent_mode import coerce_agent_mode
from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.kimi_backend import KimiBackend

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import LLMBackend


@dataclass(frozen=True)
class _BackendSpec:
    """A backend's factory plus the env contract its preflight checks read."""

    factory: Callable[[FleetConfig], LLMBackend]
    env_var: str | None = None
    key_hint: str = ""


# name → spec; mutate via register() to add backends at import time.
_REGISTRY: dict[str, _BackendSpec] = {}


def register(
    name: str,
    factory: Callable[[FleetConfig], LLMBackend],
    *,
    env_var: str | None = None,
    key_hint: str = "",
) -> None:
    """Register a backend factory under *name* (lower-cased).

    ``env_var`` is the API-key variable the doctor and CLI preflights check, and
    ``key_hint`` is extra guidance shown when it is missing. Both live here as the
    single source the preflight layer reads, so a new backend wires its key
    contract once rather than in doctor, cli_env, and pr_review.github_action.
    """
    _REGISTRY[name.lower()] = _BackendSpec(factory, env_var=env_var, key_hint=key_hint)


def backend_env_var(name: str) -> str | None:
    """API-key env var a registered backend requires, or None if unregistered."""
    spec = _REGISTRY.get(name.lower())
    return spec.env_var if spec else None


def backend_key_hint(name: str) -> str:
    """Extra guidance shown when a registered backend's API key is missing."""
    spec = _REGISTRY.get(name.lower())
    return spec.key_hint if spec else ""


def _make_cursor(config: FleetConfig) -> LLMBackend:
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


register(
    "cursor", _make_cursor, env_var="CURSOR_API_KEY", key_hint="create one at cursor.com/dashboard"
)
register("kimi", _make_kimi, env_var="KIMI_API_KEY", key_hint="Kimi Code subscription")


def make_backend(config: FleetConfig) -> LLMBackend:
    """Return the configured LLM backend."""
    name = (getattr(config, "default_backend", None) or "cursor").lower()
    spec = _REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown default_backend {name!r}. Known backends: {known}.")
    return spec.factory(config)
