"""Regression tests for `fleet --version`.

`fleet --version` must print the version string and exit 0.
Previously it errored with 'the following arguments are required: command'
because argparse was never told about a --version action.
"""

from __future__ import annotations

import pytest

import agent_fleet


def test_version_flag_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    """``main(['--version'])`` must exit 0 and print a version string."""
    from agent_fleet.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0, (
        f"expected exit code 0 for --version, got {exc.value.code!r}"
    )
    out = capsys.readouterr().out
    assert agent_fleet.__version__ in out, (
        f"version string {agent_fleet.__version__!r} not found in stdout: {out!r}"
    )


def test_version_flag_includes_program_name(capsys: pytest.CaptureFixture[str]) -> None:
    """The --version output should mention the program name."""
    from agent_fleet.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # Accept either "fleet" or "agent-fleet" as the program prefix.
    assert "fleet" in out.lower(), (
        f"expected 'fleet' in --version output, got: {out!r}"
    )


def test_normalize_argv_version_flag_passthrough() -> None:
    """normalize_argv must pass --version through unchanged (flag-first rule)."""
    from pathlib import Path

    from agent_fleet.cli_core import normalize_argv

    known: frozenset[str] = frozenset({"run", "doctor", "summon"})
    cwd = Path("/tmp")
    result = normalize_argv(["--version"], known, cwd)
    assert result == ["--version"], (
        f"normalize_argv should not mutate ['--version'], got {result!r}"
    )
