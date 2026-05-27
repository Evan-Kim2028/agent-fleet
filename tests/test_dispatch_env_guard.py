"""Tests for the dispatch.py env-var guard against silent cwd fallback."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _run_dispatch(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_fleet.issue_loop.dispatch"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dispatch_refuses_when_neither_workspace_nor_target_config_set() -> None:
    """Without AGENT_FLEET_WORKSPACE or AGENT_FLEET_TARGET_CONFIG, dispatch
    silently used Path.cwd() and could resolve the controller's own config as
    the target. Now it must exit 2 with a named error."""
    proc = _run_dispatch(
        {
            "PATH": "/usr/bin:/bin",
            "ISSUE_NUMBER": "999",
            "PERSONA": "data",
        }
    )
    assert proc.returncode == 2, proc.stderr
    assert "AGENT_FLEET_WORKSPACE" in proc.stderr
    assert "AGENT_FLEET_TARGET_CONFIG" in proc.stderr


def test_dispatch_rejects_unset_issue_number_before_env_guard() -> None:
    """ISSUE_NUMBER validation must still fire before the workspace guard;
    operator typo on issue number should yield the existing exit 1, not the
    new exit 2."""
    proc = _run_dispatch({"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 1, proc.stderr
    assert "ISSUE_NUMBER" in proc.stderr


def test_dispatch_passes_env_guard_when_workspace_set(tmp_path: Path) -> None:
    """Setting AGENT_FLEET_WORKSPACE alone (no target_config) must clear the
    guard. The dispatch itself will fail later (no .agent-fleet.yaml in
    tmp_path) — but with the existing exit 1 path, not the guard's exit 2."""
    proc = _run_dispatch(
        {
            "PATH": "/usr/bin:/bin",
            "ISSUE_NUMBER": "999",
            "PERSONA": "data",
            "AGENT_FLEET_WORKSPACE": str(tmp_path),
        }
    )
    assert proc.returncode != 2, proc.stderr
