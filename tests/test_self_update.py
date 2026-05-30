"""Tests for ``fleet self update``."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from agent_fleet.cli import cmd_self_update, main
from agent_fleet.cli_core import normalize_argv

if TYPE_CHECKING:
    import pytest


# ---------------------------------------------------------------------------
# cmd_self_update unit tests
# ---------------------------------------------------------------------------


def test_self_update_invokes_uv_tool_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess.run is called with exactly ['uv', 'tool', 'upgrade', 'agent-fleet']."""
    mock_result = MagicMock()
    mock_result.returncode = 0

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        calls.append(argv)
        return mock_result

    monkeypatch.setattr("agent_fleet.cli.subprocess.run", fake_run)

    rc = cmd_self_update(argparse.Namespace())

    assert calls == [["uv", "tool", "upgrade", "agent-fleet"]]
    assert rc == 0


def test_self_update_returns_subprocess_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return code from subprocess.run is propagated."""
    mock_result = MagicMock()
    mock_result.returncode = 42

    monkeypatch.setattr("agent_fleet.cli.subprocess.run", lambda *_a, **_kw: mock_result)

    rc = cmd_self_update(argparse.Namespace())

    assert rc == 42


def test_self_update_file_not_found_returns_nonzero_no_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FileNotFoundError (uv not installed) prints a message and returns nonzero."""

    def raise_fnf(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("uv not found")

    monkeypatch.setattr("agent_fleet.cli.subprocess.run", raise_fnf)

    rc = cmd_self_update(argparse.Namespace())

    assert rc != 0


# ---------------------------------------------------------------------------
# normalize_argv routing
# ---------------------------------------------------------------------------


def test_self_is_registered_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """'self' is a registered top-level subcommand in the built parser."""
    # Capture --help output from main, which always includes subcommand names.
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with contextlib.suppress(SystemExit):
        main(["--help"])

    help_text = buf.getvalue()
    assert "self" in help_text


def test_normalize_argv_self_update_passthrough() -> None:
    """normalize_argv passes ['self', 'update'] through unchanged when 'self' is known."""
    known: set[str] = {"run", "self", "doctor", "summon"}
    result = normalize_argv(["self", "update"], known, Path("/tmp"))
    assert result == ["self", "update"]
