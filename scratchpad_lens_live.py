"""Run ONLY the gate's lens stage against lake-of-rage PR 3541, read-only.

This is the live confirmation that the lens stage now returns findings. It
creates its own worktree at the PR head, runs the four lenses through the real
cmd backend, and writes the funnel. It never pushes and never writes to
lake-of-rage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_fleet.backends import make_backend
from agent_fleet.config import load_fleet_config
from agent_fleet.gate.config import load_gate_config
from agent_fleet.gate.gitops import PullRequestRef, prepare_worktree, remove_worktree
from agent_fleet.gate.pipeline import GatePipeline
from agent_fleet.model_policy import ModelPolicy, parse_model_policy
from agent_fleet.slots import PoolConfig, agent_slot_pool, default_slots_root

REPO = Path("/home/evan/Documents/lake-of-rage")
PR = 3541
HEAD = "bb3a90fecd5195e54a95907aec4abd2b3781543b"
GATE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/gate-lens-3541")

from agent_fleet.gate.pipeline import _load_raw_config  # noqa: E402

config_raw = _load_raw_config(None)
policy = parse_model_policy(config_raw)
cfg = load_gate_config(config_raw) or None
assert cfg is not None
# The machine-wide fleet.yaml pins no model for `cmd`; use the lane policy model
# so this live check exercises the model the fleet actually runs.
from dataclasses import replace as _replace  # noqa: E402

from agent_fleet.fleet_ops.models import CMD_MODEL  # noqa: E402

cfg = _replace(cfg, model=CMD_MODEL)
print(f"lenses={cfg.lenses} backend={cfg.backend} model={cfg.model} enable_fix={cfg.enable_fix}")

config = load_fleet_config()
config.default_backend = cfg.backend
backend = make_backend(config)

GATE_DIR.mkdir(parents=True, exist_ok=True)
wt = GATE_DIR / "wt"

pipe = GatePipeline(
    repo=REPO,
    pr_number=PR,
    config=cfg,
    policy=policy,
    backend=backend,
    gate_dir=GATE_DIR,
    agent_pool=agent_slot_pool(PoolConfig(root=default_slots_root(), agent_slots=4, test_slots=1)),
    use_systemd=False,
)

ref = PullRequestRef(number=PR, head_ref="dq1d/venueaudit", head_sha=HEAD, state="OPEN")
prepare_worktree(REPO, wt, HEAD)
try:
    findings = pipe.find(wt, ref)
finally:
    remove_worktree(REPO, wt)

print(f"\n=== {len(findings)} candidate finding(s) ===")
for f in findings:
    print(f"  [{f.lens}] {f.id} {f.file}:{f.line} testable={f.testable}")
    print(f"      {f.claim}")

print("\n=== per-lens call records ===")
for row in pipe.recorder.rows():
    print(
        f"  {row['lens']:<13} raw_len={row['raw_len']:<6} parsed_ok={row['parsed_ok']!s:<5} "
        f"n_items={row['n_items']} err={row['parse_error'][:90]!r}"
    )

Path("/tmp/lens-3541-funnel.json").write_text(
    json.dumps(
        {
            "candidates": len(findings),
            "findings": [f.to_dict() for f in findings],
            "calls": pipe.recorder.rows(),
        },
        indent=2,
        default=str,
    ),
    encoding="utf-8",
)
print("\nfunnel written to /tmp/lens-3541-funnel.json")
