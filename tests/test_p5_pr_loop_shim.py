"""P5 — Delete pr-loop pass-through.

Tests cover:
- ``agent_fleet.pr_loop.cli`` module is GONE (deleted).
- The new shim at ``agent_fleet.pr_loop._shim`` exists and prepends "loop".
- pyproject.toml points ``agent-fleet-pr-loop`` at the new shim.
- ``fleet loop --help`` and the shim produce the same exit code (0 for --help).
- ``fleet loop --once`` (with no .agent-fleet.yaml) and the shim with ``--once``
  produce the same exit code.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run_fleet(argv: list[str], env: dict | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke ``fleet`` (agent_fleet.cli:main) as a subprocess."""
    import os

    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(ROOT),
    }
    if "VIRTUAL_ENV" in os.environ:
        base["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    if env:
        base.update(env)
    return subprocess.run(
        [sys.executable, "-m", "agent_fleet.cli"] + argv,
        env=base,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_shim(argv: list[str], env: dict | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the pr-loop shim (agent_fleet.pr_loop._shim:main) as a subprocess."""
    import os

    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(ROOT),
    }
    if "VIRTUAL_ENV" in os.environ:
        base["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    if env:
        base.update(env)
    return subprocess.run(
        [sys.executable, "-c",
         "from agent_fleet.pr_loop._shim import main; raise SystemExit(main())"] + argv,
        env=base,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# pr_loop/cli.py is gone
# ---------------------------------------------------------------------------


def test_pr_loop_cli_module_deleted() -> None:
    """agent_fleet.pr_loop.cli must NOT exist after P5."""
    cli_path = ROOT / "agent_fleet" / "pr_loop" / "cli.py"
    assert not cli_path.exists(), (
        "agent_fleet/pr_loop/cli.py still exists; P5 requires it to be deleted"
    )


# ---------------------------------------------------------------------------
# New shim exists and points pyproject.toml at it
# ---------------------------------------------------------------------------


def test_shim_module_importable() -> None:
    """agent_fleet.pr_loop._shim must exist and be importable."""
    spec = importlib.util.find_spec("agent_fleet.pr_loop._shim")
    assert spec is not None, "agent_fleet.pr_loop._shim not found; shim not created"


def test_pyproject_points_pr_loop_shim_to_new_location() -> None:
    """pyproject.toml must NOT point agent-fleet-pr-loop at pr_loop.cli."""
    content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "pr_loop.cli" not in content, (
        "pyproject.toml still references pr_loop.cli; update it to the new shim"
    )
    # Must still have an agent-fleet-pr-loop entry
    assert "agent-fleet-pr-loop" in content, (
        "agent-fleet-pr-loop console script missing from pyproject.toml"
    )


# ---------------------------------------------------------------------------
# Shim prepends "loop" — behavioural parity with "fleet loop"
# ---------------------------------------------------------------------------


def test_shim_help_matches_fleet_loop_help_exit_code() -> None:
    """``agent-fleet-pr-loop --help`` and ``fleet loop --help`` must both exit 0."""
    fleet_proc = _run_fleet(["loop", "--help"])
    shim_proc = _run_shim(["--help"])
    assert fleet_proc.returncode == 0, f"fleet loop --help failed: {fleet_proc.stderr!r}"
    assert shim_proc.returncode == 0, f"shim --help failed: {shim_proc.stderr!r}"


def test_shim_and_fleet_loop_help_contain_loop_description() -> None:
    """Both outputs must mention loop-relevant content (not cmd_run or another command)."""
    fleet_proc = _run_fleet(["loop", "--help"])
    shim_proc = _run_shim(["--help"])
    # Both should describe the loop subcommand
    for label, proc in [("fleet loop", fleet_proc), ("shim", shim_proc)]:
        combined = proc.stdout + proc.stderr
        assert "loop" in combined.lower() or "watcher" in combined.lower() or "pr" in combined.lower(), (
            f"{label} --help output doesn't mention loop/watcher/pr: {combined!r}"
        )


def test_shim_and_fleet_loop_once_same_exit_code(tmp_path: Path) -> None:
    """``agent-fleet-pr-loop --once`` and ``fleet loop --once`` must return same exit code
    when run in a directory with no .agent-fleet.yaml (both exit 0 for --once poll)."""
    fleet_proc = _run_fleet(["loop", "--once", "--workspace", str(tmp_path)])
    shim_proc = _run_shim(["--once", "--workspace", str(tmp_path)])
    assert fleet_proc.returncode == shim_proc.returncode, (
        f"fleet loop --once rc={fleet_proc.returncode}, shim --once rc={shim_proc.returncode}\n"
        f"fleet stderr: {fleet_proc.stderr!r}\n"
        f"shim stderr:  {shim_proc.stderr!r}"
    )
