"""Shared GitHub CLI (gh) subprocess helpers."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def gh(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    timeout_s: int = 120,
) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        check=False,
        timeout=timeout_s,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result
