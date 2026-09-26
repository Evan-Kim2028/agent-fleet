"""Claim spec-1: `fbrun` grants --add-dir for the wrong prompts/runs tree.

`ops/vps/orchestrator/fbrun` sets

    F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
    S=${FLEET_OPS_HOME:-$HOME/fleet/ops}/..     # line 7

and then passes `--add-dir $S/fb/prompts --add-dir $S/fb/runs` (and the `..`-relative
`$S/audit`, `$S/wt/*`) to every agent it launches. But the gate that owns those artifacts,
`fbgate`, uses `F=${FLEET_OPS_HOME:-$HOME/fleet/ops}; P=$F/prompts; R=$F/runs` -- it writes
the lane task to `$F/prompts/<lane>.task.md` and reads sibling run outputs from `$F/runs`.

With the documented default (`FLEET_OPS_HOME` unset), `S/..` turns `ops` into `fleet`, so
`$S/audit` and `$S/wt/*` resolve correctly, but `$S/fb/prompts` and `$S/fb/runs` resolve to the
*sibling* `~/fleet/fb/{prompts,runs}` -- NOT the ops home `~/fleet/ops/{prompts,runs}` the gate
actually writes to. So an agent is never granted read access to the very prompt it must run or
the run outputs it must judge.

This test drives the REAL `fbrun` script (unmodified) with a stubbed `systemd-run` that records
the argv it was handed, in an isolated fake $HOME, and asserts that the prompts/runs grants the
agent actually receives resolve to the same directories `fbgate` uses. At the current head this
FAILS, because the grants point at the unrelated `fb/` sibling tree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FBRUN = REPO / "ops" / "vps" / "orchestrator" / "fbrun"

# systemd-run stub records each argv element on its own line so the --add-dir values are
# recoverable exactly as the real script produced them.
_SYSTEMD_RUN_STUB = """#!/bin/bash
for a in "$@"; do printf '%s\\n' "$a" >> "$CAPTURE_FILE"; done
exit 0
"""

# fake `ps`: report no `command-code` processes so the agent-slot gate admits immediately.
_PS_STUB = """#!/bin/bash
echo bash
"""


def _gate_ops_dir(home: Path) -> Path:
    """The prompts/runs dirs the gate uses, per the documented FLEET_OPS_HOME default."""
    return home / "fleet" / "ops"


def _setup_fake_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    ops = home / "fleet" / "ops"
    (ops / "runs").mkdir(parents=True)
    (ops / "prompts").mkdir(parents=True)
    (ops / "locks").mkdir(parents=True)
    (ops / "sem").mkdir(parents=True)
    # The sibling `fb` tree that the `$S/fb/...` grants accidentally point at.
    (home / "fleet" / "fb" / "prompts").mkdir(parents=True)
    (home / "fleet" / "fb" / "runs").mkdir(parents=True)
    (home / "fleet" / "audit").mkdir(parents=True)
    (home / "fleet" / "wt" / "silph").mkdir(parents=True)
    (home / "fleet" / "wt" / "lor").mkdir(parents=True)

    # fbrun sources $F/wait_net.sh; stub it out.
    (ops / "wait_net.sh").write_text("#!/bin/bash\nexit 0\n")
    (ops / "wait_net.sh").chmod(0o755)

    work = tmp_path / "work"
    work.mkdir()
    (work / "pf.md").write_text("review the PR\n")

    stubs = tmp_path / "stub"
    stubs.mkdir()
    (stubs / "systemd-run").write_text(_SYSTEMD_RUN_STUB)
    (stubs / "systemd-run").chmod(0o755)
    (stubs / "ps").write_text(_PS_STUB)
    (stubs / "ps").chmod(0o755)
    return home, stubs, work


def _captured_add_dirs(capture: Path) -> list[Path]:
    """Recover the value that follows each `--add-dir` in the recorded argv."""
    lines = capture.read_text().splitlines()
    out: list[Path] = []
    for i, tok in enumerate(lines[:-1]):
        if tok == "--add-dir":
            out.append(Path(lines[i + 1]))
    return out


def test_fbrun_add_dirs_target_the_ops_home_prompts_and_runs(tmp_path: Path) -> None:
    home, stubs, work = _setup_fake_home(tmp_path)
    capture = tmp_path / "capture.txt"
    capture.write_text("")

    env = {
        "HOME": str(home),
        "PATH": f"{stubs}:/usr/bin:/bin",
        "CAPTURE_FILE": str(capture),
        "FB_MODEL": "test-model",
    }
    # Deliberately do NOT set FLEET_OPS_HOME: exercise the documented default.
    env.pop("FLEET_OPS_HOME", None)

    proc = subprocess.run(
        ["bash", str(FBRUN), "rev-lane-1", str(work), str(work / "pf.md"), "50"],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    # Exit 86 (lazy-exit) is fine: the stub produced no tool calls. We only need the argv.
    assert proc.returncode in (0, 86), f"unexpected fbrun failure: {proc.returncode}\n{proc.stderr}"

    add_dirs = _captured_add_dirs(capture)
    assert add_dirs, "fbrun launched no agent with --add-dir flags"

    # The resolved directories the agent was actually granted.
    granted = {Path(os.path.normpath(d)) for d in add_dirs}
    ops = _gate_ops_dir(home)
    want_prompts = (ops / "prompts").resolve()
    want_runs = (ops / "runs").resolve()

    # The gate (`fbgate`) writes prompts to $F/prompts and reads run outputs from $F/runs.
    # The agent MUST be granted those exact directories, or it cannot read the lane prompt or
    # the sibling run results it is asked to produce/consume.
    assert want_prompts in granted, (
        f"agent was not granted the gate's prompts dir {want_prompts}; "
        f"granted={sorted(str(g) for g in granted)}"
    )
    assert want_runs in granted, (
        f"agent was not granted the gate's runs dir {want_runs}; "
        f"granted={sorted(str(g) for g in granted)}"
    )
