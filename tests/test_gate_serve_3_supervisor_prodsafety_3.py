"""prodsafety-3: an explicitly named --serve-config must fail fast.

Same rule as correctness-2, from the production-safety angle: a mistyped or
truncated ``--serve-config`` path must not leave serve running for days on
thresholds the operator believes they configured. ``_read_yaml`` returns
``None`` on OSError *and* on a YAML error, and the ``if raw is not None``
guard at config.py:371 turns both into a silent fall through to the built-in
defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.serve.config import ServeConfig, ServeConfigError, load_serve_config


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def test_missing_explicit_path_raises_instead_of_silently_defaulting() -> None:
    missing = Path("/nonexistent/serve-cfg.yaml")
    with pytest.raises(ServeConfigError):
        load_serve_config(config_path=missing)


def test_malformed_explicit_path_raises_instead_of_silently_defaulting(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "serve.yaml"
    bad.write_text("serve:\n  tick_seconds: [1, 2\n  bad: : :\n", encoding="utf-8")
    with pytest.raises(ServeConfigError):
        load_serve_config(config_path=bad)


def test_whitespace_only_explicit_path_raises(tmp_path: Path) -> None:
    empty = tmp_path / "serve.yaml"
    empty.write_text("  \n\t\n", encoding="utf-8")
    with pytest.raises(ServeConfigError):
        load_serve_config(config_path=empty)


def test_a_valid_named_file_still_works(tmp_path: Path) -> None:
    """Control: the fix must not break the legitimate explicit-config path."""
    good = tmp_path / "serve.yaml"
    good.write_text(
        "serve:\n  tick_seconds: 7\n  watchdog:\n    no_progress_minutes: 3\n",
        encoding="utf-8",
    )
    cfg: ServeConfig = load_serve_config(config_path=good)
    assert cfg.tick_seconds == 7.0
    assert cfg.watchdog.no_progress_minutes == 3
