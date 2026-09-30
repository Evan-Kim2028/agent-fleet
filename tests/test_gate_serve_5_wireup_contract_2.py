"""contract-2: a named ``--serve-config`` must never fall back to defaults.

``load_serve_config`` documents the contract precisely: an explicitly named
``config_path`` that cannot be read, is not YAML, or has no ``serve:`` section
raises :class:`ServeConfigError` — "a supervisor quietly running on thresholds
the operator believes they configured is the failure this rule exists to
prevent."

The defect under test is upstream of that: because ``--serve-config`` is
registered on both the ``serve`` parent parser and every subparser, a value
given in parent position is overwritten by the subparser's ``None`` default, so
``_load`` hands ``load_serve_config(config_path=None)``. The file is never read
and the supervisor starts on built-in defaults (tick_seconds 15.0, cgroup
``agents.slice``) with no error at all.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.cli import _load, register_serve_commands

if TYPE_CHECKING:
    from pathlib import Path
from agent_fleet.serve.config import ServeConfigError

NAMED = """\
serve:
  tick_seconds: 4242
  cgroup: my-configured-cgroup
"""


@pytest.fixture
def named_config(tmp_path: Path) -> Path:
    path = tmp_path / "serve.yaml"
    path.write_text(NAMED, encoding="utf-8")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet")
    sub = parser.add_subparsers(dest="command")
    register_serve_commands(sub)
    return parser


def _args(argv: list[str]) -> argparse.Namespace:
    return _parser().parse_args(argv)


def test_parent_position_serve_config_is_actually_read(named_config: Path) -> None:
    """`serve --serve-config FILE status` must configure from FILE."""
    args = _args(["serve", "--serve-config", str(named_config), "status"])
    config = _load(args)
    assert config.tick_seconds == 4242.0, (
        f"the named config was not read; tick_seconds={config.tick_seconds} is the "
        "built-in default, so the supervisor would run on thresholds the operator "
        "did not choose"
    )
    assert config.cgroup == "my-configured-cgroup"


def test_subcommand_position_serve_config_is_read(named_config: Path) -> None:
    """The subcommand position is the one argparse preserves — the control."""
    args = _args(["serve", "status", "--serve-config", str(named_config)])
    config = _load(args)
    assert config.tick_seconds == 4242.0
    assert config.cgroup == "my-configured-cgroup"


def test_both_positions_configure_identically(named_config: Path) -> None:
    """Flag position must not change which thresholds the supervisor runs on."""
    before = _load(_args(["serve", "--serve-config", str(named_config), "status"]))
    after = _load(_args(["serve", "status", "--serve-config", str(named_config)]))
    assert (before.tick_seconds, before.cgroup) == (after.tick_seconds, after.cgroup)
    assert before.tick_seconds == 4242.0, (
        f"parent position resolved to the built-in default {before.tick_seconds}"
    )


def test_a_named_serve_config_reaches_the_loader_in_parent_position(named_config: Path) -> None:
    """The parsed Namespace must still carry the path, not the subparser's None."""
    args = _args(["serve", "--serve-config", str(named_config), "status"])
    assert args.serve_config == str(named_config), (
        f"serve_config was clobbered to {args.serve_config!r}, so the named file is "
        "never read and no ServeConfigError can ever be raised for it"
    )


def test_a_missing_named_config_is_an_error_from_parent_position(tmp_path: Path) -> None:
    """The documented failure: an explicit path that cannot be read is an error.

    With the flag in the preserved position this already raises; the point is
    that parent position must behave the same way rather than silently
    defaulting.
    """
    missing = tmp_path / "MISSING.yaml"
    with pytest.raises(ServeConfigError):
        _load(_args(["serve", "status", "--serve-config", str(missing)]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
