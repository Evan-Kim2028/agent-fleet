"""PATH and external-tool resolution for fleet subprocesses.

Fleet often runs under stripped environments (systemd, CI, non-login shells)
where ``~/.local/bin`` is missing from PATH even though ``uv tool install``
puts binaries there. Commit preflight and git hooks need ``pre-commit`` on
PATH; this module centralizes discovery, PATH augmentation, and best-effort
install.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Common user-tool install locations (uv tool, pip --user, cargo, nvm fallbacks).
_EXTRA_BIN_DIRS = (
    Path("~/.local/bin"),
    Path("~/.cargo/bin"),
    Path("~/bin"),
    Path("~/.grok/bin"),
)


def extra_bin_dirs() -> list[Path]:
    """Resolved existing directories that may hold user-installed tools."""
    out: list[Path] = []
    for raw in _EXTRA_BIN_DIRS:
        path = raw.expanduser()
        if path.is_dir() and path not in out:
            out.append(path)
    return out


def augment_path(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of *env* (or os.environ) with user bin dirs prepended to PATH."""
    base = dict(env if env is not None else os.environ)
    current = base.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    for d in reversed(extra_bin_dirs()):
        s = str(d)
        if s not in parts:
            parts.insert(0, s)
    base["PATH"] = os.pathsep.join(parts)
    return base


def which_tool(name: str, *, env: dict[str, str] | None = None) -> str | None:
    """Locate *name* on the augmented PATH (or absolute path if *name* is absolute)."""
    candidate = Path(name)
    if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    path_env = augment_path(env)
    found = shutil.which(name, path=path_env.get("PATH"))
    if found:
        return found
    for d in extra_bin_dirs():
        direct = d / name
        if direct.is_file() and os.access(direct, os.X_OK):
            return str(direct)
    return None


def ensure_pre_commit(*, install: bool = True) -> str | None:
    """Return path to the ``pre-commit`` binary, installing via ``uv tool`` if needed.

    Returns None when the binary cannot be found (and install is disabled or fails).
    """
    existing = which_tool("pre-commit")
    if existing:
        return existing
    if not install:
        return None

    uv = which_tool("uv")
    installers: list[list[str]] = []
    if uv:
        installers.append([uv, "tool", "install", "pre-commit"])
    # Fallbacks for hosts without uv (rare for fleet operators).
    installers.append(["pipx", "install", "pre-commit"])
    installers.append(["python3", "-m", "pip", "install", "--user", "pre-commit"])

    path_env = augment_path()
    for cmd in installers:
        if which_tool(cmd[0], env=path_env) is None and not Path(cmd[0]).is_file():
            continue
        logger.info("Installing pre-commit via: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                env=path_env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("pre-commit install attempt failed (%s): %s", cmd[0], exc)
            continue
        if proc.returncode != 0:
            logger.warning(
                "pre-commit install via %s failed (rc=%s): %s",
                cmd[0],
                proc.returncode,
                (proc.stderr or proc.stdout or "")[:500],
            )
            continue
        found = which_tool("pre-commit", env=path_env)
        if found:
            logger.info("pre-commit available at %s", found)
            return found

    return which_tool("pre-commit")
