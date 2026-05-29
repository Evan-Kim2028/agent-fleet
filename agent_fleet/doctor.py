"""Onboarding preflight: a pure check set plus a text renderer.

``agent-fleet doctor`` runs these checks and prints, for each, a status and an
actionable fix when it is not a pass. The logic is kept pure and parameterized
(backend name, repo presence) so it is unit-testable without a live CLI, a real
API key, or a network round-trip.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from agent_fleet.fleet_paths import agent_fleet_home

_TARGET_PY = (3, 14)
_BACKEND_ENV = {"cursor": "CURSOR_API_KEY", "kimi": "KIMI_API_KEY"}
_STATUS_GLYPH = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str  # "pass" | "warn" | "fail"
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


def _check_python() -> DoctorCheck:
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) == _TARGET_PY:
        return DoctorCheck("Python runtime", "pass", detail)
    if (v.major, v.minor) < _TARGET_PY:
        return DoctorCheck(
            "Python runtime",
            "fail",
            f"{detail} (<3.14)",
            "agent-fleet targets Python 3.14. Install it (`uv python install 3.14`) "
            "and recreate the venv (`uv sync`).",
        )
    return DoctorCheck(
        "Python runtime",
        "warn",
        f"{detail} (>3.14, untested)",
        "agent-fleet is pinned to Python 3.14; newer versions are untested.",
    )


def _check_backend_key(backend: str) -> DoctorCheck:
    env = _BACKEND_ENV.get(backend.lower())
    if env is None:
        return DoctorCheck(
            f"{backend} API key", "warn", f"unknown backend '{backend}'; cannot verify a key"
        )
    if os.environ.get(env):
        return DoctorCheck(env, "pass", "set")
    fix = f"export {env}=<your key>"
    if backend.lower() == "cursor":
        fix += "  (create one at cursor.com/dashboard)"
    return DoctorCheck(env, "fail", "not set", fix)


def _check_cursor_sdk(backend: str) -> DoctorCheck:
    if importlib.util.find_spec("cursor_sdk") is not None:
        return DoctorCheck("cursor-sdk", "pass", "cursor_sdk importable")
    status = "fail" if backend.lower() == "cursor" else "warn"
    return DoctorCheck(
        "cursor-sdk",
        status,
        "cursor_sdk not importable",
        "Reinstall dependencies with `uv sync` (or `uv pip install cursor-sdk`).",
    )


def _check_gh() -> DoctorCheck:
    if shutil.which("gh") is None:
        return DoctorCheck(
            "GitHub CLI (gh)",
            "warn",
            "not installed",
            "Install gh (cli.github.com). Needed for issue/PR flows, not local `run`.",
        )
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DoctorCheck("GitHub CLI (gh)", "warn", f"could not run gh auth status: {exc}")
    if proc.returncode == 0:
        return DoctorCheck("GitHub CLI (gh)", "pass", "authenticated")
    return DoctorCheck(
        "GitHub CLI (gh)",
        "warn",
        "not authenticated",
        "Run `gh auth login`. Needed for issue/PR flows, not local `run`.",
    )


def _check_fleet_config() -> DoctorCheck:
    global_cfg = agent_fleet_home() / "fleet.yaml"
    if global_cfg.exists():
        return DoctorCheck("fleet config", "pass", str(global_cfg))
    return DoctorCheck(
        "fleet config",
        "warn",
        f"{global_cfg} absent (built-in defaults in use)",
        "Optional: create ~/.agent-fleet/fleet.yaml to customize fleet defaults.",
    )


def _check_repo_config(repo_present: bool | None) -> DoctorCheck | None:
    if repo_present is None:
        return None
    if repo_present:
        return DoctorCheck("repo config", "pass", ".agent-fleet.yaml found in workspace")
    return DoctorCheck(
        "repo config",
        "warn",
        "no .agent-fleet.yaml in workspace",
        "Run `agent-fleet init` to scaffold one (optional for ad-hoc runs).",
    )


def run_doctor_checks(
    *, backend: str = "cursor", repo_present: bool | None = None
) -> list[DoctorCheck]:
    """Run every preflight check. Pure aside from reading env/filesystem/gh."""
    checks = [
        _check_python(),
        _check_backend_key(backend),
        _check_cursor_sdk(backend),
        _check_gh(),
        _check_fleet_config(),
    ]
    repo = _check_repo_config(repo_present)
    if repo is not None:
        checks.append(repo)
    return checks


def doctor_exit_code(checks: list[DoctorCheck]) -> int:
    """Nonzero only when a hard requirement failed; warnings never gate."""
    return 1 if any(c.status == "fail" for c in checks) else 0


def render_doctor(checks: list[DoctorCheck]) -> str:
    """Render checks as a human-facing report. Pure: input checks -> text."""
    lines = ["agent-fleet doctor", ""]
    for c in checks:
        lines.append(f"  [{_STATUS_GLYPH.get(c.status, '?')}] {c.name:<18} {c.detail}")
        if c.status != "pass" and c.fix:
            lines.append(f"         fix: {c.fix}")
    n_fail = sum(c.status == "fail" for c in checks)
    n_warn = sum(c.status == "warn" for c in checks)
    n_pass = sum(c.status == "pass" for c in checks)
    lines.extend(["", f"{n_fail} failed, {n_warn} warnings, {n_pass} passed"])
    return "\n".join(lines)
