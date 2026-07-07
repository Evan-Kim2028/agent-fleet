"""Regression: backend registry resolves built-ins, accepts new entries, rejects unknown."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_fleet.config import load_fleet_config

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig

ROOT = Path(__file__).resolve().parent.parent

_CURSOR_SDK_AVAILABLE = importlib.util.find_spec("cursor_sdk") is not None
requires_cursor_sdk = pytest.mark.skipif(
    not _CURSOR_SDK_AVAILABLE, reason="cursor_sdk not installed — openrouter-only/kimi-only env"
)


def _config() -> FleetConfig:
    return load_fleet_config(ROOT / "fleet.example.yaml")


@requires_cursor_sdk
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


def test_openrouter_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_fleet.backends import make_backend
    from agent_fleet.openrouter_backend import OpenRouterBackend

    cfg = _config()
    monkeypatch.setattr(cfg, "default_backend", "openrouter", raising=False)
    backend = make_backend(cfg)
    assert isinstance(backend, OpenRouterBackend)


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
    assert backend_env_var("openrouter") == "OPENROUTER_API_KEY"
    assert backend_env_var("unregistered") is None


def test_builtin_backend_sdk_import_checks() -> None:
    """Cursor declares an SDK import check; kimi and openrouter do not."""
    from agent_fleet.backends import backend_sdk_import_check

    assert backend_sdk_import_check("cursor") == "cursor_sdk"
    assert backend_sdk_import_check("kimi") is None
    assert backend_sdk_import_check("openrouter") is None
    assert backend_sdk_import_check("unregistered") is None


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


def test_doctor_derives_sdk_check_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """doctor reads each backend's SDK import contract from the registry, one source.

    Registering a backend with ``sdk_import_check`` makes doctor probe that module
    with no edit to doctor. A backend with ``sdk_import_check=None`` (kimi,
    openrouter) gets no SDK check at all — an openrouter-only install never sees a
    cursor_sdk warning. This is the registry-driven counterpart to the env-var
    test above and locks the same single-source invariant for SDK availability.
    """
    from agent_fleet import backends
    from agent_fleet.doctor import run_doctor_checks

    # Backend that declares an SDK we know is absent.
    backends.register(
        "fake-with-sdk",
        lambda _cfg: object(),  # ty: ignore[invalid-argument-type]
        env_var="FAKE_WITH_SDK_KEY",
        key_hint="nowhere",
        sdk_import_check="definitely_not_a_real_module_xyz",
    )
    # Backend that declares no SDK dependency.
    backends.register(
        "fake-no-sdk",
        lambda _cfg: object(),  # ty: ignore[invalid-argument-type]
        env_var="FAKE_NO_SDK_KEY",
        key_hint="nowhere",
        sdk_import_check=None,
    )
    try:
        monkeypatch.delenv("FAKE_WITH_SDK_KEY", raising=False)
        monkeypatch.delenv("FAKE_NO_SDK_KEY", raising=False)

        # The SDK-declaring backend gets a fail check for the missing module.
        checks_with = run_doctor_checks(backend="fake-with-sdk")
        sdk_check = next(
            (c for c in checks_with if c.name == "definitely_not_a_real_module_xyz"), None
        )
        assert sdk_check is not None, (
            "doctor must emit an SDK check for a backend that declares one"
        )
        assert sdk_check.status == "fail"
        assert "not importable" in sdk_check.detail

        # The no-SDK backend gets NO SDK check — the cursor_sdk probe must not run.
        checks_without = run_doctor_checks(backend="fake-no-sdk")
        assert not any(c.name == "definitely_not_a_real_module_xyz" for c in checks_without)
        assert not any("cursor_sdk" in c.name for c in checks_without), (
            "a no-SDK backend must never trigger a cursor_sdk check"
        )
    finally:
        backends._REGISTRY.pop("fake-with-sdk", None)
        backends._REGISTRY.pop("fake-no-sdk", None)


def test_doctor_skips_sdk_check_for_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    """An openrouter-only install never sees a cursor_sdk warning from doctor."""
    from agent_fleet.doctor import run_doctor_checks

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    checks = run_doctor_checks(backend="openrouter")
    assert not any("cursor_sdk" in c.name for c in checks), (
        "openrouter backend must not trigger a cursor_sdk check"
    )


def test_import_isolation_openrouter_does_not_import_cursor_or_kimi() -> None:
    """Keystone switchability test: selecting openrouter imports only openrouter_backend.

    After ``make_backend`` with ``default_backend='openrouter'``, neither
    ``cursor_backend`` nor ``kimi_backend`` may be in ``sys.modules``. This is the
    "all or nothing" import-graph guarantee: an openrouter-only install never
    pulls in the cursor or kimi backend modules (and therefore never needs
    cursor_sdk importable or the kimi-cli binary on PATH).
    """
    _backend_mods = (
        "agent_fleet.cursor_backend",
        "agent_fleet.kimi_backend",
        "agent_fleet.openrouter_backend",
    )
    # Save original sys.modules state so we don't poison other tests' isinstance checks.
    saved = {m: sys.modules.get(m) for m in _backend_mods}
    for m in _backend_mods:
        sys.modules.pop(m, None)
    try:
        from agent_fleet.backends import make_backend
        from agent_fleet.config import FleetConfig

        cfg = FleetConfig(default_backend="openrouter", default_model=None)
        make_backend(cfg)

        assert "agent_fleet.openrouter_backend" in sys.modules, (
            "openrouter_backend was not imported"
        )
        assert "agent_fleet.cursor_backend" not in sys.modules, (
            "cursor_backend leaked into an openrouter-only make_backend call"
        )
        assert "agent_fleet.kimi_backend" not in sys.modules, (
            "kimi_backend leaked into an openrouter-only make_backend call"
        )
    finally:
        # Restore original module objects so subsequent tests' isinstance checks
        # see the same class identity. Remove any re-imported fresh modules first.
        for m in _backend_mods:
            sys.modules.pop(m, None)
        for m, obj in saved.items():
            if obj is not None:
                sys.modules[m] = obj


def test_import_isolation_kimi_does_not_import_cursor_or_openrouter() -> None:
    """Symmetric: selecting kimi imports only kimi_backend."""
    _backend_mods = (
        "agent_fleet.cursor_backend",
        "agent_fleet.kimi_backend",
        "agent_fleet.openrouter_backend",
    )
    saved = {m: sys.modules.get(m) for m in _backend_mods}
    for m in _backend_mods:
        sys.modules.pop(m, None)
    try:
        from agent_fleet.backends import make_backend
        from agent_fleet.config import FleetConfig

        cfg = FleetConfig(default_backend="kimi", default_model=None)
        make_backend(cfg)

        assert "agent_fleet.kimi_backend" in sys.modules
        assert "agent_fleet.cursor_backend" not in sys.modules
        assert "agent_fleet.openrouter_backend" not in sys.modules
    finally:
        for m in _backend_mods:
            sys.modules.pop(m, None)
        for m, obj in saved.items():
            if obj is not None:
                sys.modules[m] = obj
