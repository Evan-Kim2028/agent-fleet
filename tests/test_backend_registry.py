"""Regression: backend registry resolves built-ins, accepts new entries, rejects unknown."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.config import load_fleet_config

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig

ROOT = Path(__file__).resolve().parent.parent


def _config() -> FleetConfig:
    return load_fleet_config(ROOT / "fleet.example.yaml")


def test_cursor_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.cursor_backend import CursorBackend

    cfg = _config()
    monkeypatch.setattr(cfg, "default_backend", "cursor", raising=False)
    backend = make_backend(cfg)
    assert isinstance(backend, CursorBackend)


def test_kimi_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.kimi_backend import KimiBackend

    cfg = _config()
    monkeypatch.setattr(cfg, "default_backend", "kimi", raising=False)
    backend = make_backend(cfg)
    assert isinstance(backend, KimiBackend)


def test_registered_backend_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet import backends

    class _FakeBackend:
        def run(self, *_args: object, **_kwargs: object) -> object:
            raise NotImplementedError

    cfg = _config()
    monkeypatch.setattr(cfg, "default_backend", "fake", raising=False)
    # Register a new backend without touching make_backend's body.
    backends.register("fake", lambda _cfg: _FakeBackend())  # ty: ignore[invalid-argument-type]
    try:
        backend = backends.make_backend(cfg)
        assert isinstance(backend, _FakeBackend)
    finally:
        backends._REGISTRY.pop("fake", None)


def test_unknown_backend_raises_helpful_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.backends import make_backend

    cfg = _config()
    monkeypatch.setattr(cfg, "default_backend", "nonexistent", raising=False)
    with pytest.raises(ValueError, match="nonexistent") as exc_info:
        make_backend(cfg)
    # Error message must list known backends so the user knows what to use.
    assert "cursor" in str(exc_info.value)
    assert "kimi" in str(exc_info.value)


def test_doctor_covers_every_registered_backend() -> None:
    """A registered backend must have a real key check, not a fallback warn.

    make_backend resolves any registered backend from one file, but the backend's
    env-var contract is duplicated as a closed {cursor, kimi} set in doctor,
    cli_env, and pr_review.github_action. A third backend instantiates yet
    silently degrades there (doctor warns "unknown backend", the preflights skip
    its key check). This pins the doctor copy to the registry so that drift fails
    loudly instead of shipping a half-wired backend.
    """
    from agent_fleet import backends
    from agent_fleet.doctor import _BACKEND_ENV

    missing = set(backends._REGISTRY) - set(_BACKEND_ENV)
    assert not missing, (
        f"backends registered but absent from doctor._BACKEND_ENV: {sorted(missing)}. "
        "Add the env var there (and in cli_env and pr_review.github_action), or wire the "
        "env contract into the registry so all three derive from one source."
    )
