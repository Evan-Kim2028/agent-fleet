"""One live lens (correctness) against lake-of-rage PR 3541, read-only.

Confirms the turn-cap fix end to end: with the raised turn budget the reviewer
should now reach a verdict and report its findings, instead of being cut off at
80 turns and having the repair turn's empty list read as "no blockers".
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace as _replace
from pathlib import Path

from agent_fleet.backends import make_backend
from agent_fleet.config import load_fleet_config
from agent_fleet.fleet_ops.models import CMD_MODEL
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.gitops import PullRequestRef, prepare_worktree, remove_worktree
from agent_fleet.gate.pipeline import GatePipeline, _load_raw_config
from agent_fleet.model_policy import ModelPolicy, parse_model_policy
from agent_fleet.slots import PoolConfig, agent_slot_pool, default_slots_root

REPO = Path("/home/evan/Documents/lake-of-rage")
PR = 3541
HEAD = "bb3a90fecd5195e54a95907aec4abd2b3781543b"
GATE_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/gate-lens-3541-v2")
LENS = sys.argv[2] if len(sys.argv) > 2 else "correctness"

policy = parse_model_policy(_load_raw_config(None))
cfg = _replace(
    load_gate_config(_load_raw_config(None)) or GateConfig(),
    model=CMD_MODEL,
    lenses=(LENS,),
    enable_fix=False,
    enable_judge=False,
)
print(f"lens={LENS} backend={cfg.backend} model={cfg.model}")

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
    agent_pool=agent_slot_pool(
        PoolConfig(root=default_slots_root(), agent_slots=2, test_slots=1)
    ),
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
    print(f"      repro: {f.repro[:200]}")

print("\n=== per-lens call records ===")
for row in pipe.recorder.rows():
    print(
        f"  {row['lens']:<13} raw_len={row['raw_len']:<6} ok={row['parsed_ok']!s:<5} "
        f"n={row['n_items']} exit={row['exit_code']} dur={row['duration_s']:.0f}s"
    )

Path("/tmp/lens-3541-v2-funnel.json").write_text(
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
print("\nfunnel -> /tmp/lens-3541-v2-funnel.json")
