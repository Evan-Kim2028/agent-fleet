"""Claim correctness-1: the ops/vps parse guard only syntax-checks `*.sh` files.

The shipped guard `tests/test_ops_vps_scripts.py` selects shell scripts with
`p.suffix == ".sh"`, but the fleet's core operational scripts -- both 356-line `fbgate` copies,
`fbrun`, `fbgate_remote`, `orun`, `fbagent` and the four admission shims -- carry no `.sh`
extension. A syntactically broken copy of any of them therefore merges green: the suite stays
green while `bash -n` fails on the shipped file.

This test asserts the CORRECT invariant -- every executable shell script under `ops/vps`, however
it is named, must pass `bash -n` -- and additionally shows that the shipped guard's own
file-selection logic does not cover the extensionless ones, so a broken copy of a core script is
never flagged. It works on a temporary git worktree of HEAD (created under tmp_path and removed
afterwards) so the shared tree is never modified. At the current head this FAILS, because
corrupting an extensionless script leaves the tree un-parsed by the guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The extensionless bash scripts the claim names. Each starts with a bash shebang, so they are
# real shell scripts that `bash -n` can and should check.
UNGUARDED = [
    "ops/vps/worker/fb/fbgate",
    "ops/vps/orchestrator/fbgate",
    "ops/vps/orchestrator/fbrun",
    "ops/vps/orchestrator/fbgate_remote",
    "ops/vps/worker/bin/orun",
    "ops/vps/worker/fb/fbagent",
    "ops/vps/orchestrator/shim/git",
    "ops/vps/orchestrator/shim/uv",
    "ops/vps/worker/fb/shim/git",
    "ops/vps/worker/fb/shim/uv",
]

# A control: this one IS covered by the guard's `.sh` selection.
GUARDED_CONTROL = "ops/vps/orchestrator/wait_net.sh"

# Syntactically invalid bash; `bash -n` exits non-zero on this.
CORRUPTION = "\nif then fi fi ((( bad\n"


def _make_temp_worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    proc = subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt), "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:  # pragma: no cover - environment guard
        pytest.fail(f"could not create temp worktree: {proc.stdout}\n{proc.stderr}")
    return wt


def _cleanup_temp_worktree(wt: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _guard_shell_files(ops_vps: Path) -> list[Path]:
    """The exact selection the shipped guard uses: `p.suffix == ".sh"`."""
    return sorted(p for p in ops_vps.rglob("*") if p.suffix == ".sh" and p.is_file())


def _bash_n_ok(path: Path) -> bool:
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def test_every_ops_vps_shell_script_is_covered_by_the_parse_guard() -> None:
    """At HEAD this fails: the extensionless core scripts are not covered by the guard.

    Corrupt each named extensionless script in a throwaway worktree; then require that the
    shipped parse guard's selected set actually contains and validates them. It does not, so
    the assertion fails -- the defect the claim describes.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        wt = _make_temp_worktree(tmp_path)
        try:
            ops_vps = wt / "ops" / "vps"
            selected = {p.relative_to(ops_vps).as_posix() for p in _guard_shell_files(ops_vps)}

            # Sanity: the claimed scripts exist and are shell scripts.
            for rel in UNGUARDED:
                target = wt / rel
                assert target.is_file(), f"claimed script missing: {rel}"
                assert target.read_text(errors="replace").startswith("#!"), (
                    f"claimed script is not a script: {rel}"
                )

            # Corrupt every extensionless script plus the guarded control.
            for rel in [*UNGUARDED, GUARDED_CONTROL]:
                with (wt / rel).open("a") as fh:
                    fh.write(CORRUPTION)

            # Every corrupted script really is a bash syntax error now.
            for rel in UNGUARDED:
                assert not _bash_n_ok(wt / rel), (
                    f"{rel} should be a syntax error after corruption but bash -n accepted it"
                )

            # THE INVARIANT: the guard must cover every shell script regardless of extension.
            # The guard's selected set is supposed to contain all of them; it does not, so this
            # fails at HEAD -- proving a broken core gate/fbrun/shim merges green.
            uncovered = [rel for rel in UNGUARDED if rel not in selected]
            assert not uncovered, (
                "the parse guard selects only `*.sh`, so these extensionless core scripts are "
                f"never syntax-checked and a broken copy merges green: {uncovered}"
            )
        finally:
            _cleanup_temp_worktree(wt)
