"""correctness-2: an explicitly named --serve-config must fail fast.

``load_serve_config`` guards on ``if raw is not None`` (config.py:371), so a
named file that is missing, unreadable or empty makes ``_read_yaml`` return
``None`` and the guard is skipped entirely -- the function falls through to
``return ServeConfig(operator=operator)`` and the operator gets built-in
defaults. The module docstring states the opposite rule: "when a file is named
explicitly but has no serve section, that is an **error**, not a silent fall
back to defaults."
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.config import ServeConfigError, load_serve_config

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(tmp_path / "home" / "runs"))


def test_control_a_named_file_without_a_serve_section_does_raise(
    tmp_path: Path,
) -> None:
    """Proves the fail-fast rule is real and reachable -- so its absence is a bug."""
    path = tmp_path / "noserve.yaml"
    path.write_text("something_else: 1\n", encoding="utf-8")
    with pytest.raises(ServeConfigError):
        load_serve_config(config_path=path)


def test_control_an_explicit_path_with_a_serve_section_is_honoured(
    tmp_path: Path,
) -> None:
    path = tmp_path / "good.yaml"
    path.write_text("serve:\n  tick_seconds: 42\n", encoding="utf-8")
    cfg = load_serve_config(config_path=path)
    assert cfg.tick_seconds == 42.0


def test_control_an_implicit_source_absent_falls_back_to_defaults(
    tmp_path: Path,
) -> None:
    """Only *implicit* sources may tolerate absence."""
    cfg = load_serve_config(operator="op", repo_root=tmp_path / "no-such-repo")
    assert cfg.tick_seconds == 15.0


def test_a_named_file_that_does_not_exist_must_raise(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-here.yaml"
    with pytest.raises(ServeConfigError):
        load_serve_config(config_path=missing)


def test_a_named_file_with_malformed_yaml_must_raise(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("serve: [oops\n  - : :\n", encoding="utf-8")
    with pytest.raises(ServeConfigError):
        load_serve_config(config_path=bad)


def test_a_named_file_that_is_empty_must_raise(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ServeConfigError):
        load_serve_config(config_path=empty)
