# ops/vps — the scripts that actually run the fleet

These are the operational scripts the fleet runs day to day, checked into the repo so GitHub `main`
matches what executes. They are the *real* scripts, path-parametrised only: no behaviour was changed.

- `orchestrator/` — the laptop-side control loop (dispatch, gate, merge, deploy-verify, triage, closer).
- `worker/` — what runs on the lor-main VPS (gate port, agent route, OpenRouter engine, memory guard).
- `systemd/` — the unit files for the VPS side.

## Configuration

Every machine-specific path is a variable with a working default. Override by exporting:

| Variable | Default | Used for |
| --- | --- | --- |
| `FLEET_OPS_HOME` | `$HOME/fleet/ops` | the fleet ops dir: `lanes/`, `runs/`, `gate/`, `prompts/`, `sem/`, `events.log`, the `shim/` on `PATH` |
| `FLEET_WT_ROOT` | `$HOME/fleet/wt` | base worktrees `<repo>-wt-fleetbase`, lane worktrees `<repo>-wt-fb-<lane>` |
| `FLEET_VPS_HOST` | `lake-vps-lor-main` | ssh host alias for the VPS (remote gate, deploy verify, dbt parse) |
| `FLEET_GH_OWNER` | `Evan-Kim2028` | GitHub owner for every `gh -R <owner>/<repo>` call |

`worker/` scripts additionally read `~/fleet/env.sh` on the VPS and expect the fleet there
(`$HOME/fleet/fb`, `$HOME/fleet/runs`); the OpenRouter key comes from the environment and is never
written to disk by these scripts.

## The pieces

### orchestrator/ (laptop)

| File | What it does |
| --- | --- |
| `dispatch.py` | Queue dispatcher: runs triaged items as `fleet lane run` lanes, then queues `fbgate` per finished lane. |
| `restart_dispatch.sh` | Safely restart a dispatcher after a code change: pauses it, adopts running/finished lanes, rewrites the queue, relaunches. |
| `fbgate` | The evidence-based merge gate (tiered, see below) for one PR; writes `PREMERGE-APPROVED <sha9>` or `NEEDS-ESCALATION`. |
| `fbrun` | The agent launcher: one headless `cmd` agent (Command Code, space-bunny) under a global slot, with adaptive cap and turn/net-death recovery. |
| `fleet_reconcile.sh` | The self-healing loop: re-gates orphan/infra/rework lanes, tunes the agent cap, sheds stuck gates, closes issues every 10 min. |
| `automerge2.sh` | Batch-window merger: collects approved lanes whose PR head still matches and merges them in batch windows. |
| `lake_merge_verify.sh` | Merge one lake PR in its window; wait for `deploy-lor-api` + the lor-main verify unit; probe vs baseline; check the replica. |
| `lake_batch_merge.sh` | Merge several approved lake PRs back-to-back, then one full `lake_merge_verify` on the last. |
| `silph_merge_verify.sh` | Merge one silph PR, wait for `deploy.yml`, verify per the documents-26 procedure. |
| `silph_batch_merge.sh` | Merge approved silph PRs back-to-back, one deploy/verify on the last. |
| `fastmerge_ext.sh` | documents-1d fast path: merge-tree + importing tests + fence check, then merge+verify (no model review). |
| `fbgate_remote` | Start one gate on the VPS (remote gate worker) and return; refuses if the lane is already gating there. |
| `remote_sync.sh` | Copy new remote gate status lines back into the local `lanes/` and `events.log`; refresh `remote_live.txt`. |
| `rebase_regate.sh` | Rebase a conflicting lane onto main, then re-gate (or carry the approval over a patch-identical rebase). |
| `pr_triage.sh` / `pr_triage.py` | Deterministic (no-model) triage of open `fb/*` PRs: close ON-MAIN/SUPERSEDED/DEAD, rebase CONFLICTING. |
| `close_issues.py` | Close issues that merged PRs declare fixed (GitHub ignores the `lake#N` shorthand, so this does it). |
| `dbt_parse_check.sh` | `dbt parse --target prod` of (main + SHA) in a scratch worktree on the VPS. |
| `fm_pytest.sh` | Run tests grouped by nearest `pyproject.toml`, memory-capped, prints `FAILED <id>` lines. |
| `wait_net.sh` | Network backpressure: hold new agent starts until GitHub + model endpoints are healthy. |
| `lean_gate_claims.py` | Render gate claims as one line each for the batched verifier prompt. |
| `prompts/lean_verify_batch.md` | The batched-verifier prompt (one test per claim). |
| `shim/git` | Admission shim: refuses `--no-verify` and any `core.hooksPath` write/disable. |
| `shim/uv` | Admission shim: slot-limits `pytest`/`pyright`, refuses whole-suite runs. |

### worker/ (lor-main VPS)

| File | What it does |
| --- | --- |
| `fb/fbgate` | The same evidence gate, ported to the VPS: agents via `fbagent`, everything in `fleet.slice`. |
| `fb/fbagent` | Remote agent launcher with the `fbrun` contract. Default route Command Code; `FBAGENT_ROUTE=openrouter` opts into the OpenRouter engine (falls back to `cmd` on API/rate-limit failure). |
| `bin/orun` / `bin/orun.py` | The OpenRouter engine: one agent via `agent_fleet.openrouter_backend`, same `runs/NAME.*` contract. |
| `bin/fleet-pressure-guard.sh` | Graduated memory guard v3 for `fleet.slice` (see memory model). |
| `fb/shim/git`, `fb/shim/uv` | The same admission/hook-safety shims, on `PATH` for VPS agents. |

### systemd/

| File | What it does |
| --- | --- |
| `fleet.slice` | The cgroup slice that bounds the whole fleet. |
| `fleet-pressure-guard.service` / `.timer` | Runs the guard every 30 s. |

## Data flow

```
dispatch.py ──▶ fleet lane run (opens fb/<lane> PR)
      │
      ▼
   fbgate  (local)  ─ or ─▶  fbgate_remote ──▶ fbgate (worker) via fbagent
      │                     remote_sync.sh copies status back
      │  PREMERGE-APPROVED <sha9>  |  NEEDS-ESCALATION
      ▼
fleet_reconcile.sh  (re-gates orphan/infra/rework, closes issues, tunes cap)
      │
      ▼
automerge2.sh  (batch window: BATCH_MIN or BATCH_WAIT_S)
      │
      ├──▶ lake_batch_merge.sh / lake_merge_verify.sh   ──▶ deploy-lor-api ──▶ lor-main verify + probe
      └──▶ silph_batch_merge.sh / silph_merge_verify.sh ──▶ deploy.yml ──▶ prod verify (documents-26)
      │
      ▼
pr_triage.sh (close dead/superseded/conflicted PRs)   close_issues.py (close fixed issues)
```

## Knobs

Many knobs are **files** under `$FLEET_OPS_HOME` (write a number into one and the next tick picks it
up) rather than env vars — that is how they are tuned live.

**Fleet-wide (files under `$FLEET_OPS_HOME`)**

| Knob | Default | Effect |
| --- | --- | --- |
| `agent_total_max` | 80 (AIMD 30–90) | soft whole-fleet cap on concurrent agent processes; tuned by `fleet_reconcile.sh` from network deaths + GitHub connect time. |
| `agent_slots` | 56 | advisory slot-pool size (admission is really the `agent_total_max` process count). |
| `agent_reserved_late` | 10 | slots/cap reserved for late gate stages (fixers, judges, verifiers) so they never starve behind new reviewers. |
| `gate_wip` | 35 | max in-flight local gates before new gates wait (`MAXGATES` env overrides). |
| `remote_gate_slots` | 10 | max gates on the VPS; `0` disables remote gating. |
| `local_agents_off` | absent | presence pins the local agent cap to 0 (all agents on the VPS). |
| `lake_hold` | absent | presence blocks all lake merges (except `LAKE_HOLD_OVERRIDE=1`). |
| `batch_mode` | absent | presence defers silph merges to a batch (`FORCE_MERGE=1` overrides). |

**Gate (`fbgate`)**

| Knob | Default | Effect |
| --- | --- | --- |
| `GATE_TIER0` | 1 | docs/tests-only PRs are approved on step-0 tests + merged-tree check, no model review. `0` disables. |
| `GATE_LENSES` | auto | `1` = one all-focus reviewer (lean default); `4` = the four parallel lenses. Auto: `4` when the non-test diff is big or production-sensitive. |
| `GATE_BIG_LINES` | 600 | non-test diff line count that forces the 4-lens tier. |
| `GATE_VERIFY_BATCH` | 1 | one batched verifier per gate instead of one per claim. |
| `GATE_MAX_FIX_ROUNDS` | 4 | safety-net cap on fix rounds (the real stop rule is convergence). |
| `GATE_AGENT_SLOTS` | 124 | agent slots available to the gate. |
| `GATE_JUDGE` / `GATE_JUDGE_EFFORT` | `cmd` / `high` | final judge route; `grok` (step-5-preview) opts in. |

**Batch merge / deploy**

| Knob | Default | Effect |
| --- | --- | --- |
| `BATCH_MIN` | 5 | approved PRs needed before a repo's batch fires. |
| `BATCH_WAIT_S` | 1800 | or wait this long for the oldest approved PR. |
| `LAKE_HOLD_OVERRIDE` | unset | let a merge through while `lake_hold` is set. |
| `LAKE_DEPLOY_FREEZE_MAX_S` | 2700 | auto-thaw `agents-adhoc.slice` after this long during a deploy window. |

**Reconciler / agents**

| Knob | Default | Effect |
| --- | --- | --- |
| `STUCK_SECS` | 5400 | kill + re-gate a gate whose status hasn't moved in this long. |
| `NET_MAX_CONNECT` | 0.8 | GitHub connect-time ceiling before new gates are deferred. |
| `TEST_SLOTS` | 10 | concurrent `fm_pytest` runs (each capped at 6G). |
| `AGENT_TEST_SLOTS` / `AGENT_TYPECHECK_SLOTS` | 12 (laptop) / 4,3 (VPS) | `uv` shim slot counts for pytest / pyright. |
| `FB_MODEL` / `FB_MODEL_OR` | `stealth/space-bunny-alpha` | agent model. |
| `FB_CAP` | by name (90m–5h) | per-agent wall-clock cap. |
| `FBAGENT_ROUTE` | `cmd` | VPS agent route; `openrouter` uses the OpenRouter engine. |
| `NET`/agent routes | — | `FB_OR_MAX_TOKENS`, `FB_OR_TIMEOUT_S`, `ALLOW_FULL_SUITE` also honoured. |

## VPS memory model

The whole fleet shares one cgroup slice, `fleet.slice`
(`MemoryHigh=20G`, `MemoryMax=22G`, `MemorySwapMax=0`, `CPUQuota=600%`, `CPUWeight=20`,
`IOWeight=20`, `TasksMax=4000`). Swap is disabled so reclaim is fast and visible, and the low
CPU/IO weights mean the fleet yields to interactive and production work.

`fleet-pressure-guard.sh` (v3) runs every 30 s and **throttles rather than freezes**, graduating by
host state (`MemAvailable` GiB and memory-PSI `full avg60`):

| Level | Trigger | Action |
| --- | --- | --- |
| 0 | calm | admission open; `CPUQuota=600%`, `MemoryHigh` as configured. |
| 1 | avail < 12G or PSI > 10% | **admission closed** — no new gates start; running ones continue. |
| 2 | avail < 8G or PSI > 25% | **throttle** — `CPUQuota=200%`, `MemoryHigh` pinned at current usage (slows work, never stops it). |
| 3 | avail < 4G | **shed** — stop the newest remote gate each tick (frees memory; the orchestrator re-queues it). |
| 4 | avail < 2G | **freeze** `fleet.slice` — last resort before the kernel OOM killer. |

The guard relaxes one level at a time after 5 calm minutes, logs every change, and exports its level
and admission state under `~/fleet/state/` where `fbgate_remote` and the orchestrator read it.
