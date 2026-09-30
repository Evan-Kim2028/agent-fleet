"""spec-2: the same invocation must behave identically in either flag position.

The defect is the duplicate ``dest`` registration: ``add_common`` puts
``--operator``/``--serve-config``/``--repo-root`` on the ``serve`` parent parser
*and* on every subparser, and argparse then resolves the subparser's own
default onto the shared ``dest``. Two invocations that differ only in where the
flag sits therefore diverge — one errors, the other succeeds — and neither the
operator nor the machine has any way to tell which spelling they used.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import pytest

from agent_fleet.serve.cli import cmd_serve_status, register_serve_commands

if TYPE_CHECKING:
    from pathlib import Path

MISSING = "/tmp/definitely-not-a-real-fleet-serve-config.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet")
    sub = parser.add_subparsers(dest="command")
    register_serve_commands(sub)
    return parser


def _run_status(argv: list[str], capsys: pytest.CaptureFixture[str]) -> int:
    """Drive cmd_serve_status and return its exit code, capturing stdout."""
    args = _parser().parse_args(argv)
    code = cmd_serve_status(args)
    capsys.readouterr()
    return code


def test_operator_before_the_subcommand_is_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`serve --operator evan status` must not report the operator missing."""
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    code = _run_status(["serve", "--operator", "evan", "status"], capsys)
    assert code == 0, (
        "the subparser's empty --operator default overwrote the parsed value, so "
        "cmd_serve_status exited 2 with '--operator is required'"
    )


def test_a_missing_serve_config_errors_from_either_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable named config is an error — but from *both* positions.

    Subcommand position already errors. Parent position silently drops the path
    and prints a normal status screen, which is the silent-defaults failure the
    module docstring promises cannot happen.
    """
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    after = _run_status(
        ["serve", "status", "--serve-config", MISSING, "--operator", "evan"], capsys
    )
    before = _run_status(
        ["serve", "--serve-config", MISSING, "status", "--operator", "evan"], capsys
    )
    assert after == 2, "control: the preserved position must reject an unreadable config"
    assert before == 2, (
        "the parent's --serve-config was overwritten by the subparser's None default, "
        "so the unreadable path was never seen and the command exited 0"
    )


def test_serve_config_survives_the_subparser_in_parent_position() -> None:
    """The parsed Namespace must still hold the path in parent position."""
    args = _parser().parse_args(
        ["serve", "--serve-config", "/tmp/x.yaml", "status", "--operator", "e"]
    )
    assert args.serve_config == "/tmp/x.yaml", f"clobbered to {args.serve_config!r}"


def test_the_two_orderings_agree_on_the_operator() -> None:
    before = _parser().parse_args(["serve", "--operator", "evan", "status"])
    after = _parser().parse_args(["serve", "status", "--operator", "evan"])
    assert before.operator == after.operator == "evan"


def test_top_level_run_is_unaffected_by_the_serve_registration() -> None:
    """A control: the control path is the one argparse does not clobber."""
    args = _parser().parse_args(["serve", "run", "--operator", "evan", "--max-ticks", "1"])
    assert args.operator == "evan"
    assert args.max_ticks == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
