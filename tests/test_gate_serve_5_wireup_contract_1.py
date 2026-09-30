"""contract-1: a flag before the subcommand must survive the subparser.

``add_common`` registers ``--operator``/``--serve-config``/``--repo-root`` on
the ``serve`` parent parser *and* on every subparser. argparse resolves a
subparser's own defaults onto the same ``dest`` after the parent has finished
parsing, so the subparser's default overwrites whatever the parent parsed and
the flag is silently discarded. A flag that is honoured in one position and
silently dropped in the other is the defect: ``fleet serve --operator op run``
must behave the same as ``fleet serve run --operator op``.
"""

from __future__ import annotations

import argparse

import pytest

from agent_fleet.serve.cli import register_serve_commands


def _parser() -> argparse.ArgumentParser:
    """The ``fleet`` parser, shaped like the real one, with serve registered."""
    parser = argparse.ArgumentParser(prog="fleet")
    sub = parser.add_subparsers(dest="command")
    register_serve_commands(sub)
    return parser


def test_operator_before_the_subcommand_is_not_discarded() -> None:
    """`serve --operator alice status` must still see alice."""
    parser = _parser()
    args = parser.parse_args(["serve", "--operator", "alice", "status"])
    assert args.operator == "alice", (
        "the subparser's --operator default ('') overwrote the value the parent "
        f"parser parsed; got {args.operator!r}"
    )


def test_run_accepts_the_operator_before_the_subcommand() -> None:
    """The documented no-subcommand-adjacent form: `serve --operator op run`."""
    parser = _parser()
    args = parser.parse_args(["serve", "--operator", "op", "run", "--max-ticks", "1"])
    assert args.operator == "op", f"got {args.operator!r}"
    assert args.max_ticks == 1


def test_flag_position_is_irrelevant_to_the_parsed_operator() -> None:
    """Both orderings must agree — that is the whole contract."""
    parser = _parser()
    before = parser.parse_args(["serve", "--operator", "documents-0e", "status"])
    after = parser.parse_args(["serve", "status", "--operator", "documents-0e"])
    assert before.operator == after.operator == "documents-0e"


def test_a_parent_operator_reaches_the_command_without_reparsing() -> None:
    """The Namespace handed to the command must carry the operator.

    ``cmd_serve_status``/``cmd_serve_run`` read ``args.operator`` and exit 2
    with "--operator is required" when it is empty, so a dropped operator is
    not merely cosmetic — it fails the command outright.
    """
    parser = _parser()
    args = parser.parse_args(["serve", "--operator", "evan", "status"])
    assert args.operator, "cmd_serve_* would exit 2 with '--operator is required'"


def test_the_other_common_flags_are_clobbered_the_same_way() -> None:
    """--repo-root shares the registration, so it must be pinned too."""
    parser = _parser()
    args = parser.parse_args(["serve", "--repo-root", "/tmp/somewhere", "status"])
    assert args.repo_root == "/tmp/somewhere", f"got {args.repo_root!r}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
