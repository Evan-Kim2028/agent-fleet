"""Table-driven unit tests for agent_fleet.cli_core.normalize_argv.

Documented cases:
  empty       -> ["summon"]
  known cmd   -> passthrough (no mutation)
  unknown token (non-flag, non-subcommand) -> prepend "run"
  flag-first  -> passthrough (no mutation; flag-only invocation is the parser's concern)
  goal equals subcommand name -> document that "fleet run <goal>" is the unambiguous form

All tests are purely functional — no I/O, no filesystem, no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_fleet.cli_core import normalize_argv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KNOWN: frozenset[str] = frozenset(
    {
        "run",
        "review",
        "scope",
        "scout",
        "personas",
        "doctor",
        "runs",
        "watch",
        "loop",
        "init",
        "bridge",
        "level-up",
        "learn",
        "summon",
        "workstream",
        "dag",
        # P3 additions (not yet wired, but we document them as known)
        "pr-analyze",
        "dispatch",
        "schedule",
    }
)

_CWD = Path("/tmp/test-cwd")


# ---------------------------------------------------------------------------
# Empty argv -> summon
# ---------------------------------------------------------------------------


def test_empty_argv_returns_summon() -> None:
    """Bare invocation (no args) launches summon."""
    assert normalize_argv([], _KNOWN, _CWD) == ["summon"]


# ---------------------------------------------------------------------------
# Known subcommand -> passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "fix the bug"],
        ["review", "--workspace", "/tmp/repo"],
        ["doctor"],
        ["doctor", "--json"],
        ["loop", "--once"],
        ["init", "/tmp/repo"],
        ["learn", "--dry-run"],
        ["summon"],
        ["bridge", "start"],
        ["level-up", "status", "--repo", "."],
        ["workstream", "plan"],
    ],
)
def test_known_subcommand_passthrough(argv: list[str]) -> None:
    """First token is a known subcommand → argv unchanged."""
    assert normalize_argv(argv, _KNOWN, _CWD) == argv


# ---------------------------------------------------------------------------
# Unknown first token (non-flag) -> prepend "run"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected",
    [
        # Plain goal string
        (
            ["fix the bug in auth.py"],
            ["run", "fix the bug in auth.py"],
        ),
        # Goal with extra options
        (
            ["refactor the login module", "--persona", "coder"],
            ["run", "refactor the login module", "--persona", "coder"],
        ),
        # Multi-word goal as single arg (the user quoted it)
        (
            ["write unit tests for scheduler"],
            ["run", "write unit tests for scheduler"],
        ),
    ],
)
def test_unknown_first_token_prepends_run(argv: list[str], expected: list[str]) -> None:
    """Non-flag, non-subcommand first token -> prepend 'run'."""
    result = normalize_argv(argv, _KNOWN, _CWD)
    assert result == expected


# ---------------------------------------------------------------------------
# Flag-first argv -> passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--version"],
        ["--help"],
        ["--config", "/tmp/fleet.yaml", "run", "my goal"],
    ],
)
def test_flag_first_passthrough(argv: list[str]) -> None:
    """When argv[0] starts with '-', normalize_argv must not mutate argv.

    Flag-first invocations fall through to the argparse parser as-is; it is
    the parser's responsibility to handle --help, --version, etc.
    """
    assert normalize_argv(argv, _KNOWN, _CWD) == argv


# ---------------------------------------------------------------------------
# Collision: goal token equals a subcommand name
# ---------------------------------------------------------------------------


def test_goal_that_equals_subcommand_name_routes_to_run() -> None:
    """If the user literally types 'agent-fleet doctor' it is treated as a
    known subcommand (passthrough). To run a *task* whose goal text is 'doctor'
    the unambiguous form is 'agent-fleet run doctor'.

    This test documents the collision rule: the token 'doctor' alone is
    interpreted as the subcommand, not as a goal routed to 'run'.
    """
    # Bare known subcommand name → treated as subcommand, not a run goal.
    assert normalize_argv(["doctor"], _KNOWN, _CWD) == ["doctor"]

    # Explicit 'run' prefix → unambiguous, even when goal text equals subcommand.
    assert normalize_argv(["run", "doctor"], _KNOWN, _CWD) == ["run", "doctor"]


def test_unknown_token_that_happens_to_look_like_a_flag_passes_through() -> None:
    """Single token starting with '--' is flag-first, not run-routed."""
    assert normalize_argv(["--unknown-flag"], _KNOWN, _CWD) == ["--unknown-flag"]


# ---------------------------------------------------------------------------
# Idempotency: already-routed argv stays identical
# ---------------------------------------------------------------------------


def test_already_run_prefixed_is_passthrough() -> None:
    """['run', 'some goal'] is already valid; must not double-prepend."""
    argv = ["run", "implement oauth"]
    assert normalize_argv(argv, _KNOWN, _CWD) == argv
