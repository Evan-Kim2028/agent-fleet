"""contract_1: the built-in default gate command must be a command that exists.

Claim under test
----------------
``dispatch.gate_argv(None, ...)`` is the documented default gate
(``docs/FLEET-OPS.md``, "### --gate-cmd": *"With no --gate-cmd, a lane that
produced a PR goes to the built-in agent-fleet gate"*). The argv it builds is::

    ['agent-fleet', 'gate', '--lane', L, '--repo', S, '--pr', P, '--head-ref', R]

but the real ``agent-fleet gate`` subparser only accepts ``--repo-path``,
``--pr``, ``--task-file`` and ``--status-file``. So every default gate dies in
argparse with exit 2 and no PR is ever reviewed; ``_reap_gate`` then finds no
approval line, classifies every lane ``escalated (no approval line)``, and
``exit_code()`` is 1 for every run.

The assertions below ask the real parser whether it understands the argv and the
real entry point whether it reaches ``cmd_gate``. They are the contract, not a
restatement of today's output: an implementation that emits ``--repo-path`` (or
any other supported flag) passes.
"""

from __future__ import annotations

import argparse
import contextlib
import io

import pytest

from agent_fleet import cli
from agent_fleet.fleet_ops.dispatch import gate_argv


class _ReachedGate(Exception):
    """Raised by the stand-in ``cmd_gate`` so gate reachability is observable."""


@pytest.fixture
def gate_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the gate command with a sentinel and collect the parsed args."""
    seen: list[object] = []

    def _sentinel(args: object) -> int:
        seen.append(args)
        raise _ReachedGate

    monkeypatch.setattr(cli, "cmd_gate", _sentinel)
    return seen


def _run_cli(argv: list[str]) -> object:
    """Run the real CLI over *argv*.

    Returns the stand-in gate's marker when the gate command was reached, the
    argparse exit code when the command was rejected, ``None`` otherwise.
    """
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        try:
            cli.main(list(argv))
        except _ReachedGate:
            return _ReachedGate
        except SystemExit as exc:
            return exc.code
    return None


def _gate_subparser() -> argparse.ArgumentParser:
    """The live ``gate`` subparser, captured from the parser ``cli.main`` builds."""
    captured: dict[str, argparse.ArgumentParser] = {}
    original = argparse.ArgumentParser.parse_args

    def _spy(
        self: argparse.ArgumentParser,
        *args: object,  # noqa: ARG001
        **kwargs: object,  # noqa: ARG001
    ) -> None:
        captured["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = _spy  # type: ignore[method-assign]
    try:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.suppress(SystemExit),
        ):
            cli.main(["gate", "--help"])
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]

    assert "parser" in captured, "could not capture the CLI parser"
    for action in captured["parser"]._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "gate" in choices:
            return choices["gate"]
    msg = "the CLI has no `gate` subcommand"
    raise AssertionError(msg)


def test_the_default_gate_argv_is_understood_by_the_gate_parser() -> None:
    """The dispatcher's own argv must not be rejected by argparse.

    Exit code 2 is the observable failure: it means the command the dispatcher
    spawns cannot start, so the gate never reviews anything.
    """
    argv = gate_argv(None, lane="alpha", pr=42, repo="acme", slug="Evan-Kim2028/acme")
    assert argv[:2] == ["agent-fleet", "gate"], "the default gate is the built-in one"

    result = _run_cli(argv[1:])

    assert result is _ReachedGate, (
        f"the default gate argv {argv[1:]} did not reach the gate command "
        f"(got {result!r}); `agent-fleet gate` would exit before reviewing the PR"
    )


def test_the_default_gate_argv_uses_only_flags_the_gate_subparser_defines() -> None:
    """Every ``--flag`` in the argv must be one the gate subparser declares."""
    gate_parser = _gate_subparser()
    accepted = {opt for action in gate_parser._actions for opt in action.option_strings}
    accepted |= {"-h", "--help"}

    argv = gate_argv(
        None,
        lane="alpha",
        pr=42,
        repo="acme",
        slug="Evan-Kim2028/acme",
        judge_engine="cmd",
    )
    unknown = sorted({tok for tok in argv if tok.startswith("--") and tok not in accepted})

    assert not unknown, (
        f"gate_argv(None, ...) emits {unknown}, which `agent-fleet gate` does not "
        f"define; it accepts {sorted(accepted - {'-h', '--help'})}"
    )


def test_a_gate_argv_the_parser_understands_reaches_the_gate(
    gate_spy: list[object],
) -> None:
    """Control: the failure above is the argv, not the harness.

    A gate argv built from the flags the parser really defines gets all the way
    to ``cmd_gate``. Without this control, a test that merely always fails would
    prove nothing; here the identical code path succeeds for a well-formed argv.
    """
    argv = ["gate", "--pr", "42", "--repo-path", "."]

    result = _run_cli(argv)

    assert result is _ReachedGate, f"a well-formed gate argv should reach cmd_gate, got {result!r}"
    assert gate_spy, "cmd_gate should have been called"
