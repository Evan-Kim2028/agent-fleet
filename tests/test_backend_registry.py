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


def test_builtin_backend_env_vars() -> None:
    from agent_fleet.backends import backend_env_var

    assert backend_env_var("cursor") == "CURSOR_API_KEY"
    assert backend_env_var("kimi") == "KIMI_API_KEY"
    assert backend_env_var("unregistered") is None


def test_doctor_derives_key_check_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """doctor reads each backend's env contract from the registry, one source.

    Registering a backend with an env_var makes doctor check that exact key with
    no edit to doctor, cli_env, or github_action. This locks the single-source
    invariant the env-metadata registry establishes: a new backend wires its key
    contract once, at the register() call.
    """
    from agent_fleet import backends
    from agent_fleet.doctor import run_doctor_checks

    backends.register(
        "fake",
        lambda _cfg: object(),  # ty: ignore[invalid-argument-type]
        env_var="FAKE_API_KEY",
        key_hint="from nowhere",
    )
    try:
        monkeypatch.delenv("FAKE_API_KEY", raising=False)
        checks = run_doctor_checks(backend="fake")
        match = next((c for c in checks if c.name == "FAKE_API_KEY"), None)
        assert match is not None
        assert match.status == "fail"
        assert "from nowhere" in match.fix
    finally:
        backends._REGISTRY.pop("fake", None)
