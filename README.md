# Agent Fleet

**Local swarm of scoped Cursor agents on your git repos** — parallel dispatch, diff review, PR analysis, and optional watchers that fix PRs while you work elsewhere.

Built on **[Cursor SDK](https://github.com/cursor/cursor-sdk)** (`cursor-sdk`). Each agent is a **Composer** session (scoped paths, MCPs, durable multi-phase runs). Dispatch via **CLI**, **Python**, or **GitHub issue comments**.

| Docs | |
|------|---|
| [Quickstart](docs/QUICKSTART.md) | First run in ~15 minutes |
| [New repo setup](docs/NEW-REPO.md) | `.agent-fleet.yaml`, GHA, PR loop |
| [Personas](docs/PERSONAS.md) | Fleet cookbook |
| [Schedules](docs/SCHEDULES.md) | Cron-based daily/weekly fleet jobs |

**Requires:** Python 3.14 · [Cursor API key](https://cursor.com/dashboard/integrations) · git workspace  
**Default model:** `composer-2.5-fast` (use `composer-2.5` when you want the full model)

---

## What you get

| Capability | Summary |
|------------|---------|
| **Parallel implementers** | Up to `max_parallel` Composer agents; same-repo tasks auto-isolate in git worktrees |
| **In-pipeline review** | `code_review`: implement → scope → verify → **reviewer verdict** (`approve` / `request_changes` / `block`) |
| **PR analyzer** | Two-pass **Composer PR review** — CLI (`agent-fleet review`), GHA ([`pr-analyzer.yml`](examples/github/pr-analyzer.yml)), feeds PR loop |
| **Background modes** | PR loop watcher, issue-comment dispatch, **cron schedules**, parallel Python batch |
| **Structured logs** | JSONL at `~/.hermes/fleet/runs/<run-id>.jsonl` |

Typical focused task on **`composer-2.5-fast`**: **~30–120 seconds** (implement + gates; PR analysis scales with diff size).

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

```bash
git clone https://github.com/Evan-Kim2028/agent-fleet.git
cd agent-fleet
pip install -e ".[dev]"    # or: uv sync --frozen --group dev

export CURSOR_API_KEY=your_key_here
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml
# ~/.hermes/coding_fleet/ = global fleet config (personas, max_parallel), not your repo
# edit fleet.yaml: default_model: composer-2.5-fast
```

### 2. Verify install

```bash
agent-fleet personas    # should list coder, reviewer, pr-analyzer, …
```

### 3. Add your repo (recommended before real work)

**Fast path:** skip to step 4 — any git repo works as `--workspace` for a smoke test.

**Proper path:** scaffold per-repo config (scope, verify commands, optional PR loop):

```bash
export REPO=/absolute/path/to/your/repo   # must be a git checkout

agent-fleet init "$REPO"
# creates $REPO/.agent-fleet.yaml — edit persona_scope_allowlist, test_command, lint_command
```

Details: [docs/NEW-REPO.md](docs/NEW-REPO.md).

### 4. First task

**Implement + review** (~30–120s on `composer-2.5-fast`):

```bash
agent-fleet run "Add a one-line project description to README" \
  --workspace "$REPO" \
  --pipeline code_review
```

**PR review only** (working tree vs `main`):

```bash
agent-fleet review --workspace "$REPO" --format json
```

Expect JSON with `status: completed` or a typed failure (`scope_violation`, `verify_failed`, `review_changes_requested`). Commit or stash local changes in the target repo before dispatch if you want a clean diff.

---

## Running in the background

| Mode | Entry | Behavior |
|------|-------|----------|
| One-shot | `agent-fleet run …` | Single job → JSON |
| Parallel batch | `dispatch_tasks(…)` / `FleetDispatcher` | N concurrent agents (worktree per same-repo task) |
| PR analyzer (CI) | `examples/github/pr-analyzer.yml` | Composer posts structured review comment on every PR |
| PR loop watcher | `agent-fleet loop` / `agent-fleet-pr-loop` | Poll `fleet/*` PRs → fix findings → CI → optional merge |
| Issue trigger | `agent-fleet-watch` | `/agent --persona …` on issue comments → full pipeline |

**Concurrency** (`~/.hermes/coding_fleet/fleet.yaml`) — starting point for a typical 16–32 GB laptop:

```yaml
default_backend: cursor
default_model: composer-2.5-fast
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

Outcomes: `completed`, `scope_violation`, `verify_failed`, `review_changes_requested`, `review_blocked`, `error`, `decompose_partial`, `decompose_failed`.

**Orchestration (v0.6.4+):** When the planner returns `decompose`, the fleet automatically fans out `child_issues_proposed` as parallel scoped tasks (default pipeline: `code_review`). Enable via `.agent-fleet.yaml`:

```yaml
orchestration:
  enabled: true
  auto_dispatch_children: true
  preflight_on_code_review: true   # plan before code_review execute
  default_child_pipeline: code_review
```

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

Registry: `~/.hermes/coding_fleet/fleet.yaml`. Bundled prompts in `agent_fleet/personas/` (`coder`, `reviewer`, `pr-analyzer`, …). Repo `personas/` and `.agent-fleet.yaml` scope override global `allowed_paths`.

```yaml
default_backend: cursor
default_model: composer-2.5-fast
personas:
  backend:
    prompt: coder.md
    model: composer-2.5-fast
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

1. **GHA** runs `agent-fleet-pr-analyzer` → posts Composer PR analysis comment.
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
2. Default to `code_review` for merge-bound work; `composer-2.5-fast` for throughput.
3. Pass file paths and verify commands in `--context`.
4. Parallelize independent packages — never two agents on the same file.

---

## Optional: Kimi · Hermes

Cursor-first. Same personas/pipelines if you swap backend or add a plugin:

| | Setup |
|---|--------|
| [Kimi Code CLI](docs/KIMI.md) | `default_backend: kimi`, `KIMI_API_KEY` |
| [Hermes plugin](integrations/hermes/) | `./scripts/deploy-hermes.sh` |

Not required for CLI, Python, or watcher workflows.
