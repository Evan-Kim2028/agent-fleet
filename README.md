# Agent Fleet

**Local swarm of scoped Cursor agents on your git repos** — parallel dispatch, diff review, PR analysis, and optional watchers that fix PRs while you work elsewhere.

Built on **[Cursor SDK](https://github.com/cursor/cursor-sdk)** (`cursor-sdk`). Each agent is a **Composer** session (scoped paths, MCPs, durable multi-phase runs). Dispatch via **CLI**, **Python**, or **GitHub issue comments**.

| Docs | |
|------|---|
| [Quickstart](docs/QUICKSTART.md) | First run in ~15 minutes |
| [Fleet config](docs/FLEET-CONFIG.md) | Global paths, `personas_dir`, import shadow |
| [New repo setup](docs/NEW-REPO.md) | `.agent-fleet.yaml`, GHA, PR loop |
| [Personas](docs/PERSONAS.md) | Fleet cookbook |
| [Schedules](docs/SCHEDULES.md) | Cron-based daily/weekly fleet jobs |

**Requires:** Python 3.14 · [Cursor API key](https://cursor.com/dashboard/integrations) · git workspace  
**Default model:** `composer-2.5` (slow / non-fast tier — agent-fleet pins `fast=false` explicitly so you aren't silently routed to the fast variant that Cursor returns as its default for the bare model id).

> **Switch to `composer-2.5-fast`** for higher throughput at lower quality (agent-fleet pins `fast=true`). Edit `~/.agent-fleet/fleet.yaml` — set `default_model: composer-2.5-fast` to apply fleet-wide, or set `model: composer-2.5-fast` under a single persona (under `personas:`) to scope the override.

---

## What you get

| Capability | Summary |
|------------|---------|
| **Parallel implementers** | Up to `max_parallel` Composer agents; same-repo tasks auto-isolate in git worktrees |
| **In-pipeline review** | `code_review`: implement → scope → verify → **reviewer verdict** (`approve` / `request_changes` / `block`) |
| **PR analyzer** | Two-pass **Composer PR review** — CLI (`fleet review`), GHA ([`pr-analyzer.yml`](examples/github/pr-analyzer.yml)), feeds PR loop |
| **Background modes** | PR loop watcher, issue-comment dispatch, **cron schedules**, parallel Python batch |
| **Structured logs** | JSONL at `~/.agent-fleet/fleet/runs/<run-id>.jsonl` |

Typical focused task on **`composer-2.5`**: **~30–120 seconds** (implement + gates; PR analysis scales with diff size).

---

## Who this is for

- **Parallel fixes** — backend + frontend + tests in different packages at once.
- **Reviewed merges** — mechanical scope + your test commands + structured reviewer before land.
- **PR babysitting** — GHA posts Composer analysis; local watcher dispatches fix agents until CI is green.
- **Scripted dispatch** — CI, cron, or `dispatch_tasks()` with scoped personas.

Runs on your laptop, dev box, or CI runner.

---

## How it works

```
CLI / Python / watcher
        │
        ▼
  FleetDispatcher ── max_parallel admission ── worktree isolation
        │
        ├── coder (Composer)     ── implement, scoped paths
        ├── reviewer (Composer) ── diff review in code_review pipeline
        ├── pr-analyzer (Composer) ── PR diff analysis (CLI / GHA / pr_loop)
        └── …
        ▼
  JSON result · git branch/PR · JSONL logs
```

**Personas** = markdown prompts + optional path allowlists. **Pipelines** = phase order. **`.agent-fleet.yaml`** = verify commands, scope, PR loop, PR review config.

---

## Quick start

Use **absolute paths** to your target repo. You do not clone agent-fleet into that repo — fleet is a global CLI that points at any git workspace.

### 1. Install fleet (once per machine)

Agent-fleet is pinned to **Python 3.14** (`requires-python = ">=3.14,<3.15"`). Use `uv` to install the right interpreter and sync the lockfile:

```bash
uv python install 3.14
git clone https://github.com/Evan-Kim2028/agent-fleet.git
cd agent-fleet
uv sync

export CURSOR_API_KEY=your_key_here
mkdir -p ~/.agent-fleet
cp fleet.example.yaml ~/.agent-fleet/fleet.yaml
# ~/.agent-fleet/fleet.yaml = global fleet config (personas, max_parallel), not your repo
# edit fleet.yaml: default_model: composer-2.5
```

Verify the install:

```bash
fleet --help
```

To upgrade fleet to the latest published version:

```bash
fleet self update
```

This runs `uv tool upgrade agent-fleet` under the hood. Requires `uv` on `PATH`.

> **Import shadow:** Do not clone into `~/Documents/agent_fleet` (underscore). That path name matches the Python package and can shadow the installed `agent_fleet` module when used as cwd or on `PYTHONPATH`. Prefer `~/agent-fleet-dev` or any hyphenated path. Check with `python3 scripts/check-import-shadow.py` — see [docs/FLEET-CONFIG.md](docs/FLEET-CONFIG.md#import-shadow).

### 2. Preflight check

`summon` runs environment checks and prints an actionable fix for each item not passing, then prints a ready banner:

```bash
fleet summon
```

Or run checks directly:

```bash
fleet doctor
```

Checks: Python version, backend API key (`CURSOR_API_KEY`), `cursor-sdk` import, `gh` CLI, fleet config, and repo config. The command exits non-zero only on a hard failure (missing key, wrong Python version); warnings about optional items still exit 0. For CI or scripted onboarding, use machine-readable output:

```bash
fleet doctor --json
```

### 3. Add your repo (recommended before real work)

**Fast path:** skip to step 4 — any git repo works as `--workspace` for a smoke test.

**Proper path:** scaffold per-repo config (scope, verify commands, optional PR loop):

```bash
export REPO=/absolute/path/to/your/repo   # must be a git checkout

fleet init "$REPO"
# creates $REPO/.agent-fleet.yaml — edit persona_scope_allowlist, test_command, lint_command
```

Details: [docs/NEW-REPO.md](docs/NEW-REPO.md).

### 4. First task

**Implement + review** (~30–120s on `composer-2.5`):

```bash
fleet run "Add a one-line project description to README" \
  --workspace "$REPO" \
  --pipeline code_review
```

**PR review only** (working tree vs `main`):

```bash
fleet review --workspace "$REPO" --format json
```

Expect JSON with `status: completed` or a typed failure (`scope_violation`, `verify_failed`, `review_changes_requested`). Commit or stash local changes in the target repo before dispatch if you want a clean diff.

**Preview a run without spending tokens** — `--dry-run` resolves the plan (persona, pipeline, workspace, backend) and prints it as JSON, then exits before requiring a backend API key:

```bash
fleet run "Add error handling to auth module" --dry-run
```

---

## Monitoring runs

`runs` lists every recorded run, newest first:

```bash
fleet runs
```

Each row shows: id, status, tokens, started, goal. Add `--json` for machine-readable output; `--limit N` to cap rows.

`watch` tails a single run as a live phase/agent tree:

```bash
fleet watch            # defaults to 'latest'
fleet watch <run-id>   # full id or a unique prefix
```

- `--once` prints one snapshot and exits (no live loop).
- `--json` emits the complete folded run state as JSON and exits.

---

## Running in the background

| Mode | Entry | Behavior |
|------|-------|----------|
| One-shot | `fleet run …` | Single job → JSON |
| Parallel batch | `dispatch_tasks(…)` / `FleetDispatcher` | N concurrent agents (worktree per same-repo task) |
| PR analyzer (CI) | `examples/github/pr-analyzer.yml` | Composer posts structured review comment on every PR |
| PR loop watcher | `fleet loop` | Poll `fleet/*` PRs → fix findings → CI → optional merge |
| Issue trigger | `fleet dispatch` | `/agent --persona …` on issue comments → full pipeline |

**Concurrency** (`~/.agent-fleet/fleet.yaml`) — starting point for a typical 16–32 GB laptop:

```yaml
default_backend: cursor
default_model: composer-2.5
max_parallel: 6              # concurrent Composer agents; lower on 8 GB machines
max_redispatches: 1          # retry hard failures with handoff context
timeout_seconds: 900
```

MCP + persistent sessions: [docs/SESSIONS.md](docs/SESSIONS.md) · [docs/MCP.md](docs/MCP.md).

---

## Pipelines

| Pipeline | Phases | Use when |
|----------|--------|----------|
| `simple` | execute | Trivial edits |
| `code_review` | execute → scope → verify → review | Default for merge-bound work |
| `pr_review` | analyze | PR diff only (no implement) |
| `full` | PLAN → … → REVIEW → TECH_LEAD? | Large features, branch + PR |

Outcomes: `completed`, `scope_violation`, `verify_failed`, `review_changes_requested`, `review_blocked`, `error`, `decompose_partial`, `decompose_failed`, `dag_partial`, `dag_failed`.

**Orchestration (v0.6.4+):** When the planner returns `decompose`, the fleet automatically fans out `child_issues_proposed` as parallel scoped tasks (default pipeline: `code_review`). Enable via `.agent-fleet.yaml`:

```yaml
orchestration:
  enabled: true
  auto_dispatch_children: true
  auto_dispatch_dag: true
  preflight_on_code_review: true   # plan before code_review execute
  default_child_pipeline: code_review
  default_dag_pipeline: code_review
  dag_upstream_context_chars: 2000
```

**DAG task runner:** For work with explicit dependencies (research ∥ research → implement → integrate), use cookbook-compatible DAG JSON:

```bash
# Terminal ASCII diagram (default for validate / dry-run / post-run)
fleet dag validate --file examples/dag/example_dag.json
fleet dag validate --file examples/dag/example_dag.json --json   # machine-readable

# Execute (add --canvas for Cursor IDE live graph)
fleet dag run --file examples/dag/example_dag.json --workspace .
fleet dag run --file examples/dag/example_dag.json --canvas dag-run --init-only
# Open the printed canvas path in Cursor, then run without --init-only
```

**Visualization:** `dag validate` prints an ASCII rank/edge diagram for terminals and CI. Pass `--canvas` or `--canvas-path` on `dag run` to write a cookbook-compatible `.canvas.tsx` that hot-reloads in the Cursor IDE (optional; no effect in headless runs).

Ranks execute in parallel within each wave; upstream outputs are stitched into downstream prompts; failed nodes skip transitive dependents. The planner can also return `decomposition_decision: dag` with a `dag` object when `preflight_on_code_review` is enabled.

---

## Repo config (`.agent-fleet.yaml`)

| Field | Purpose |
|-------|---------|
| `persona_scope_allowlist` | Path prefixes per persona — **highest-leverage guardrail** |
| `test_command` / `lint_command` | Post-implement verification |
| `pr_review` | PR analyzer thresholds, comment title, overlay prompts |
| `pr_loop` | Local watcher: review fix → CI fix → merge |
| `code_review.auto_fix` | Re-dispatch on `request_changes` |

Examples: [`examples/repo.agent-fleet.yaml`](examples/repo.agent-fleet.yaml) · [`examples/repo-full.agent-fleet.yaml`](examples/repo-full.agent-fleet.yaml).

---

## Personas

Registry: `~/.agent-fleet/fleet.yaml` (see [docs/FLEET-CONFIG.md](docs/FLEET-CONFIG.md)). Bundled prompts in `agent_fleet/personas/` (`coder`, `reviewer`, `pr-analyzer`, …). Repo `personas/` and `.agent-fleet.yaml` scope override global `allowed_paths`.

```yaml
default_backend: cursor
default_model: composer-2.5
personas:
  backend:
    prompt: coder.md
    model: composer-2.5
    allowed_paths: ["api/", "src/"]
  pr-analyzer:
    prompt: pr-analyzer.md
    model: composer-2.5
    mode: plan
```

Cookbook: [docs/PERSONAS.md](docs/PERSONAS.md).

---

## Python API

```python
from agent_fleet import dispatch_tasks

results = dispatch_tasks(
    goal="Fix login validation",
    workspace="/path/to/repo",
    pipeline="code_review",
)
```

---

## PR loop + Composer review (typical setup)

1. **GHA** runs `fleet pr-analyze` → posts Composer PR analysis comment.
2. **Local watcher** reads that comment, dispatches fix agents, waits for CI, merges.

```yaml
# .agent-fleet.yaml
pr_loop:
  enabled: true
  branch_prefixes: [fleet/]
  fix_persona: coder
  auto_merge: true
```

Requires `gh` auth + `CURSOR_API_KEY`. Systemd example: [`examples/agent-fleet-pr-loop.service`](examples/agent-fleet-pr-loop.service).

---

## Tips

1. Scope every persona (`persona_scope_allowlist`).
2. Default to `code_review` for merge-bound work; `composer-2.5` for throughput.
3. Pass file paths and verify commands in `--context`.
4. Parallelize independent packages — never two agents on the same file.

---

## Optional: Kimi · gateway plugin

Cursor-first. Same personas/pipelines if you swap backend or add a Discord gateway plugin:

| | Setup |
|---|--------|
| [Kimi Code CLI](docs/KIMI.md) | `default_backend: kimi`, `KIMI_API_KEY` |
| [cursor-fleet plugin](integrations/hermes/) | `./scripts/deploy-hermes.sh` — fleet config stays in `~/.agent-fleet/fleet.yaml` |

Not required for CLI, Python, or watcher workflows.
