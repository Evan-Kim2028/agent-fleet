"""Guards on the operational scripts in ops/vps/.

These scripts run the fleet for real, so the repo must never drift into shipping a
broken or leaky copy: every shell script has to parse, every Python file has to
compile, and no file may carry a hard-coded machine path or a secret.
"""

from __future__ import annotations

import py_compile
import re
import subprocess
from pathlib import Path

import pytest

OPS_VPS = Path(__file__).resolve().parent.parent / "ops" / "vps"

BASH_SHEBANGS = ("#!/usr/bin/env bash", "#!/bin/bash")


def _is_shell_script(path: Path) -> bool:
    """A shell script is anything `bash` can be pointed at: a `*.sh` file, or an extensionless
    script that opens with a bash shebang. The fleet's core scripts -- both `fbgate` copies,
    `fbrun`, `fbgate_remote`, `orun`, `fbagent` and the admission shims -- carry no extension,
    so a `.sh`-only selection never syntax-checks them and a broken copy merges green.
    """
    if path.suffix == ".sh":
        return True
    text = path.read_text(errors="replace")
    return text.startswith(BASH_SHEBANGS)


SHELL_FILES = sorted(p for p in OPS_VPS.rglob("*") if p.is_file() and _is_shell_script(p))
PYTHON_FILES = sorted(p for p in OPS_VPS.rglob("*.py") if p.is_file())
ALL_FILES = sorted(p for p in OPS_VPS.rglob("*") if p.is_file())

# Paths that must never be committed: the scratchpad of one machine, and its home.
MACHINE_PATHS = ("/tmp/claude-1000/", "/home/evan/")

# Credential shapes. The scripts legitimately mention the *names* of key variables
# (OPENROUTER_API_KEY) and a redaction regex, but never a value.
SECRET_PATTERNS = {
    "openrouter key": r"sk-or-v1-[A-Za-z0-9]{16,}",
    "github classic PAT": r"ghp_[A-Za-z0-9]{20,}",
    "github fine-grained PAT": r"github_pat_[A-Za-z0-9_]{20,}",
    "bearer token": r"Bearer\s+[A-Za-z0-9._-]{20,}",
    "aws access key": r"AKIA[0-9A-Z]{16}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}

# An assignment that looks like it carries a literal value, e.g. API_KEY=abc123.
ASSIGNED_SECRET = re.compile(
    r"(?i)\b[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*"
    r"\s*=\s*['\"]?(?![$%\s])[A-Za-z0-9/+_-]{8,}"
)


def test_ops_vps_has_scripts() -> None:
    assert SHELL_FILES, f"no *.sh found under {OPS_VPS}"
    assert PYTHON_FILES, f"no *.py found under {OPS_VPS}"


@pytest.mark.parametrize("script", SHELL_FILES, ids=lambda p: p.name)
def test_shell_script_parses(script: Path) -> None:
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"bash -n {script.name} failed: {proc.stderr}"


@pytest.mark.parametrize("script", PYTHON_FILES, ids=lambda p: p.name)
def test_python_script_compiles(script: Path, tmp_path: Path) -> None:
    target = tmp_path / f"{script.stem}.pyc"
    try:
        py_compile.compile(str(script), cfile=str(target), doraise=True)
    except py_compile.PyCompileError as exc:  # pragma: no cover - failure path
        pytest.fail(f"py_compile {script.name} failed: {exc}")


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: str(p.relative_to(OPS_VPS)))
def test_no_hard_coded_machine_paths(path: Path) -> None:
    text = path.read_text(errors="replace")
    hits = [m for m in MACHINE_PATHS if m in text]
    assert not hits, f"{path.name} still hard-codes {hits}; use the FLEET_* variables"


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: str(p.relative_to(OPS_VPS)))
def test_no_secrets(path: Path) -> None:
    text = path.read_text(errors="replace")
    for name, pattern in SECRET_PATTERNS.items():
        match = re.search(pattern, text)
        assert match is None, f"{path.name} contains a {name}: {match.group(0)[:12]}..."
    assigned = ASSIGNED_SECRET.search(text)
    assert assigned is None, f"{path.name} assigns a literal credential: {assigned.group(0)}"
