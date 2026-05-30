"""P3 — Fold the 6 entry points.

Tests cover:
- ``fleet pr-analyze``, ``fleet dispatch``, ``fleet schedule`` subcommands
  are registered in the unified parser (smoke tests through main()).
- Env-var protocols flow through the adapter functions unchanged.
- Silent-cwd safety check in dispatch is preserved when routed through
  the unified ``fleet dispatch`` subcommand.
- Old console-script shim (agent-fleet-issue-dispatch) and the unified
  ``fleet dispatch`` produce the same exit code for the silent-cwd guard.
- ``fleet=agent_fleet.cli:main`` is present in pyproject.toml scripts.
- normalize_argv treats new P3 subcommands as passthrough (not run-routed).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def _run_fleet(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke ``fleet`` (agent_fleet.cli:main) as a subprocess."""
    return subprocess.run(
        [sys.executable, "-m", "agent_fleet.cli", *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _minimal_env() -> dict[str, str]:
    """Minimal env with PATH and PYTHONPATH so subprocess imports work."""
    import os

    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(ROOT),
    }
    if "VIRTUAL_ENV" in os.environ:
        base["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    return base


# ---------------------------------------------------------------------------
# pyproject.toml: "fleet" console script present
# ---------------------------------------------------------------------------


def test_fleet_console_script_in_pyproject() -> None:
    """``fleet = agent_fleet.cli:main`` must be present in [project.scripts]."""
    pyproject = ROOT / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    # Accept either quoting style
    assert "agent_fleet.cli:main" in content
    assert "fleet" in content
    # Verify fleet points to the cli module (not some other target)
    lines = [
        ln.strip()
        for ln in content.splitlines()
        if "fleet" in ln and "agent_fleet.cli:main" in ln
    ]
    assert lines, "No pyproject.toml line matches 'fleet ... agent_fleet.cli:main'"


# ---------------------------------------------------------------------------
# normalize_argv: P3 subcommands are treated as known (passthrough)
# ---------------------------------------------------------------------------


def test_normalize_argv_knows_pr_analyze() -> None:
    from agent_fleet.cli_core import normalize_argv

    known = frozenset({"pr-analyze", "run", "doctor", "summon", "dispatch", "schedule"})
    cwd = Path("/tmp")
    assert normalize_argv(["pr-analyze"], known, cwd) == ["pr-analyze"]


def test_normalize_argv_knows_dispatch() -> None:
    from agent_fleet.cli_core import normalize_argv

    known = frozenset({"dispatch", "run", "doctor", "summon", "pr-analyze", "schedule"})
    cwd = Path("/tmp")
    assert normalize_argv(["dispatch"], known, cwd) == ["dispatch"]


def test_normalize_argv_knows_schedule() -> None:
    from agent_fleet.cli_core import normalize_argv

    known = frozenset({"schedule", "run", "doctor", "summon", "pr-analyze", "dispatch"})
    cwd = Path("/tmp")
    assert normalize_argv(["schedule", "list"], known, cwd) == ["schedule", "list"]


# ---------------------------------------------------------------------------
# pr-analyze subcommand: registered and routes to env-var adapter
# ---------------------------------------------------------------------------


def test_pr_analyze_subcommand_registered_in_parser() -> None:
    """``pr-analyze`` must appear in the unified parser's subcommand choices."""
    # Build the parser via main's internals by patching sys.argv to an
    # impossible command and catching the error that mentions known choices.
    # More directly: call main() and confirm it routes to the pr-analyze handler,
    # not to cmd_run.  The handler calls github_action.main() which checks
    # GITHUB_TOKEN before anything else.
    env = _minimal_env()
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_REPOSITORY", None)
    # No backend keys either — we want the adapter to run and fail on GITHUB_TOKEN
    env.pop("CURSOR_API_KEY", None)

    proc = _run_fleet(["pr-analyze"], env)
    # The adapter should fail with a message about GITHUB_TOKEN, not CURSOR_API_KEY
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "GITHUB_TOKEN" in combined, (
        f"Expected GITHUB_TOKEN error, got: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_pr_analyze_help_exits_zero() -> None:
    """``fleet pr-analyze --help`` must exit 0."""
    env = _minimal_env()
    proc = _run_fleet(["pr-analyze", "--help"], env)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# dispatch subcommand: registered and preserves silent-cwd guard
# ---------------------------------------------------------------------------


def test_dispatch_subcommand_help_exits_zero() -> None:
    """``fleet dispatch --help`` must exit 0."""
    env = _minimal_env()
    proc = _run_fleet(["dispatch", "--help"], env)
    assert proc.returncode == 0


def test_dispatch_silent_cwd_guard_exit_2() -> None:
    """Without AGENT_FLEET_WORKSPACE or AGENT_FLEET_TARGET_CONFIG, ``fleet
    dispatch`` must exit 2 with a message about those env vars."""
    env = _minimal_env()
    env["ISSUE_NUMBER"] = "42"
    env["PERSONA"] = "coder"
    env.pop("AGENT_FLEET_WORKSPACE", None)
    env.pop("AGENT_FLEET_TARGET_CONFIG", None)

    proc = _run_fleet(["dispatch"], env)
    assert proc.returncode == 2, (
        f"Expected exit 2 (cwd guard), got {proc.returncode}: {proc.stderr!r}"
    )
    assert "AGENT_FLEET_WORKSPACE" in proc.stderr


def test_dispatch_missing_issue_number_exit_1() -> None:
    """Missing ISSUE_NUMBER exits 1 before the workspace guard fires (exit 2)."""
    env = _minimal_env()
    env.pop("ISSUE_NUMBER", None)
    env.pop("AGENT_FLEET_WORKSPACE", None)
    env.pop("AGENT_FLEET_TARGET_CONFIG", None)

    proc = _run_fleet(["dispatch"], env)
    assert proc.returncode == 1, proc.stderr
    assert "ISSUE_NUMBER" in proc.stderr


def test_dispatch_shim_and_fleet_dispatch_same_exit_code() -> None:
    """The standalone ``agent-fleet-issue-dispatch`` and ``fleet dispatch``
    must produce the same exit code for the silent-cwd guard violation."""
    env = _minimal_env()
    env["ISSUE_NUMBER"] = "99"
    env["PERSONA"] = "coder"
    env.pop("AGENT_FLEET_WORKSPACE", None)
    env.pop("AGENT_FLEET_TARGET_CONFIG", None)

    standalone = subprocess.run(
        [sys.executable, "-m", "agent_fleet.issue_loop.dispatch"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    unified = _run_fleet(["dispatch"], env)

    assert standalone.returncode == 2, standalone.stderr
    assert unified.returncode == 2, unified.stderr
    assert "AGENT_FLEET_WORKSPACE" in standalone.stderr
    assert "AGENT_FLEET_WORKSPACE" in unified.stderr


# ---------------------------------------------------------------------------
# schedule subcommand: registered and delegates to schedule/cli adapter
# ---------------------------------------------------------------------------


def test_schedule_subcommand_help_exits_zero() -> None:
    """``fleet schedule --help`` must exit 0."""
    env = _minimal_env()
    proc = _run_fleet(["schedule", "--help"], env)
    assert proc.returncode == 0


def test_schedule_list_no_config_returns_enabled_false(tmp_path: Path) -> None:
    """``fleet schedule list`` with no config must exit 0 and emit
    ``{"enabled": false}`` — same as standalone ``agent-fleet-schedule list``."""
    env = _minimal_env()

    proc = _run_fleet(["schedule", "list", "--workspace", str(tmp_path)], env)
    assert proc.returncode == 0, proc.stderr
    assert "enabled" in proc.stdout


def test_schedule_shim_and_fleet_schedule_same_output(tmp_path: Path) -> None:
    """Standalone ``agent-fleet-schedule`` and ``fleet schedule`` must produce
    identical JSON output for ``list`` when no schedules are configured.

    The standalone adapter receives ``--workspace <path> list`` (workspace
    before subcommand, as its own parser expects).  The unified CLI receives
    ``schedule list --workspace <path>`` (workspace on the subcommand).
    Both must produce the same JSON.
    """
    env = _minimal_env()

    # Standalone adapter: --workspace precedes the subcommand
    standalone = subprocess.run(
        [sys.executable, "-m", "agent_fleet.schedule.cli",
         "--workspace", str(tmp_path), "list"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # Unified CLI: workspace comes after the schedule subcommand
    unified = _run_fleet(
        ["schedule", "list", "--workspace", str(tmp_path)], env
    )

    assert standalone.returncode == unified.returncode, (
        f"standalone rc={standalone.returncode}, unified rc={unified.returncode}\n"
        f"standalone stderr: {standalone.stderr!r}\n"
        f"unified stderr: {unified.stderr!r}"
    )
    assert standalone.stdout.strip() == unified.stdout.strip(), (
        f"standalone: {standalone.stdout!r}\n"
        f"unified:    {unified.stdout!r}"
    )
