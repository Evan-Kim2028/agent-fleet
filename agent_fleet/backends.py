"""Backend factory — registry-driven; Cursor, Kimi, OpenRouter, and Grok Build CLI.

Backends are imported lazily inside their factory functions so that selecting one
backend (e.g. ``default_backend: openrouter``) does not import the others' modules
or their SDK dependencies. An all-in-on-openrouter install never imports
``cursor_backend`` or ``kimi_backend``. See ``test_import_isolation`` for the
regression gate on this invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_fleet.agent_mode import coerce_agent_mode

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import LLMBackend


@dataclass(frozen=True)
class _BackendSpec:
    """A backend's factory plus the env/SDK/auth contracts its preflight checks read.

    ``sdk_import_check`` is the importable module name the doctor probes for SDK
    availability (``None`` for backends with no SDK dependency, e.g. HTTP backends).
    ``auth_probe`` is an optional callable returning ``(ok, detail, fix)`` for
    backends that authenticate outside env vars (e.g. Grok subscription via
    ``~/.grok/auth.json``). Like ``env_var``/``key_hint``, these live here as the
    single source the preflight layer reads, so a new backend wires its contract
    once at the ``register()`` call rather than in doctor.
    """

    factory: Callable[[FleetConfig], LLMBackend]
    env_var: str | None = None
    key_hint: str = ""
    sdk_import_check: str | None = None
    auth_probe: Callable[[], tuple[bool, str, str]] | None = None


# name → spec; mutate via register() to add backends at import time.
_REGISTRY: dict[str, _BackendSpec] = {}


def register(
    name: str,
    factory: Callable[[FleetConfig], LLMBackend],
    *,
    env_var: str | None = None,
    key_hint: str = "",
    sdk_import_check: str | None = None,
    auth_probe: Callable[[], tuple[bool, str, str]] | None = None,
) -> None:
    """Register a backend factory under *name* (lower-cased).

    ``env_var`` is the API-key variable the doctor and CLI preflights check, and
    ``key_hint`` is extra guidance shown when it is missing. ``sdk_import_check``
    is the importable module name the doctor probes for SDK availability (``None``
    for backends with no SDK dependency). ``auth_probe`` is an optional callable
    returning ``(ok, detail, fix)`` for subscription/file-based auth (e.g. Grok).
    All live here as the single source the preflight layer reads, so a new backend
    wires its key + SDK + auth contract once rather than in doctor, cli_env, and
    pr_review.github_action.
    """
    _REGISTRY[name.lower()] = _BackendSpec(
        factory,
        env_var=env_var,
        key_hint=key_hint,
        sdk_import_check=sdk_import_check,
        auth_probe=auth_probe,
    )


def backend_env_var(name: str) -> str | None:
    """API-key env var a registered backend requires, or None if unregistered."""
    spec = _REGISTRY.get(name.lower())
    return spec.env_var if spec else None


def backend_key_hint(name: str) -> str:
    """Extra guidance shown when a registered backend's API key is missing."""
    spec = _REGISTRY.get(name.lower())
    return spec.key_hint if spec else ""


def backend_sdk_import_check(name: str) -> str | None:
    """Importable module name the doctor probes for SDK availability, or None."""
    spec = _REGISTRY.get(name.lower())
    return spec.sdk_import_check if spec else None


def backend_auth_probe(name: str) -> Callable[[], tuple[bool, str, str]] | None:
    """Optional auth probe for a registered backend (subscription/file auth)."""
    spec = _REGISTRY.get(name.lower())
    return spec.auth_probe if spec else None


def backend_is_registered(name: str) -> bool:
    """True if *name* is present in the backend registry."""
    return name.lower() in _REGISTRY


def _make_cursor(config: FleetConfig) -> LLMBackend:
    # Lazy import: an openrouter-only or kimi-only install never imports cursor_backend
    # (and therefore never needs cursor_sdk importable at module load).
    from agent_fleet.cursor_backend import DEFAULT_MODEL as CURSOR_DEFAULT_MODEL
    from agent_fleet.cursor_backend import CursorBackend

    return CursorBackend(
        default_model=config.default_model or CURSOR_DEFAULT_MODEL,
        default_mode=coerce_agent_mode(config.default_mode),
    )


def _make_kimi(config: FleetConfig) -> LLMBackend:
    from agent_fleet.kimi_backend import DEFAULT_MODEL as KIMI_DEFAULT_MODEL
    from agent_fleet.kimi_backend import KimiBackend

    return KimiBackend(
        model=config.default_model or KIMI_DEFAULT_MODEL,
        kimi_bin=getattr(config, "kimi_bin", None),
    )


def _make_openrouter(config: FleetConfig) -> LLMBackend:
    from agent_fleet.openrouter_backend import DEFAULT_MODEL as OPENROUTER_DEFAULT_MODEL
    from agent_fleet.openrouter_backend import OPENROUTER_BASE_URL as OPENROUTER_DEFAULT_BASE_URL
    from agent_fleet.openrouter_backend import OpenRouterBackend

    return OpenRouterBackend(
        model=config.default_model or OPENROUTER_DEFAULT_MODEL,
        base_url=getattr(config, "openrouter_base_url", None) or OPENROUTER_DEFAULT_BASE_URL,
    )


def _make_grok(config: FleetConfig) -> LLMBackend:
    from agent_fleet.grok_backend import DEFAULT_MODEL as GROK_DEFAULT_MODEL
    from agent_fleet.grok_backend import GrokBackend

    return GrokBackend(
        model=config.default_model or GROK_DEFAULT_MODEL,
        grok_bin=getattr(config, "grok_bin", None),
    )


def _grok_auth_probe() -> tuple[bool, str, str]:
    from agent_fleet.grok_backend import check_grok_auth

    return check_grok_auth()


# coerce_agent_mode is imported eagerly at the top (pure helper, no SDK dep).

register(
    "cursor",
    _make_cursor,
    env_var="CURSOR_API_KEY",
    key_hint="create one at cursor.com/dashboard",
    sdk_import_check="cursor_sdk",
)
register(
    "kimi",
    _make_kimi,
    env_var="KIMI_API_KEY",
    key_hint="Kimi Code subscription",
    sdk_import_check=None,
)
register(
    "openrouter",
    _make_openrouter,
    env_var="OPENROUTER_API_KEY",
    key_hint="create one at openrouter.ai/keys",
    sdk_import_check=None,
)
register(
    "grok",
    _make_grok,
    env_var=None,
    key_hint="run `grok login` (SuperGrok / X Premium+)",
    sdk_import_check=None,
    auth_probe=_grok_auth_probe,
)


def make_backend(config: FleetConfig) -> LLMBackend:
    """Return the configured LLM backend."""
    name = (getattr(config, "default_backend", None) or "cursor").lower()
    spec = _REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(f"Unknown default_backend {name!r}. Known backends: {known}.")
    return spec.factory(config)
