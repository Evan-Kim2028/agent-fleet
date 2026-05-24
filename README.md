# Agent Fleet

**Run a local swarm of scoped coding agents on your git repos** — in parallel, with review gates, and optional long-running watchers that babysit PRs while you work elsewhere.

Built on the **[Cursor SDK](https://github.com/cursor/cursor-sdk)** (`cursor-sdk`): each fleet agent is a **Cursor Composer** session with scoped paths, MCP tools, and durable multi-phase conversations. Dispatch from the **CLI**, **Python**, or **GitHub issue comments**.

| Docs | |
|------|---|
| [Quickstart](docs/QUICKSTART.md) | First run in ~15 minutes |
| [New repo setup](docs/NEW-REPO.md) | `.agent-fleet.yaml`, GHA, PR loop |
| [Personas](docs/PERSONAS.md) | Fleet cookbook |
| [Release tags](docs/RELEASE.md) | How we cut versions |

---

## Who this is for

You want **several non-interactive Cursor agents working locally** on real code — not a single chat session:

- **Parallel fixes** — backend + frontend + tests in different packages at once (`max_parallel`, git worktree per task).
- **Reviewed changes** — implement → scope check → your test commands → structured reviewer verdict before merge.
- **Background babysitting** — a watcher on your machine polls fleet PRs or issue comments and dispatches fix agents until CI is green (optional auto-merge).
- **Scripted dispatch** — your CI, cron jobs, or Python scripts call `agent-fleet run` / `dispatch_tasks()` with scoped personas.

Runs on **your laptop, dev box, or CI runner**. Requires a [Cursor API key](https://cursor.com/dashboard/integrations), Python 3.14, and a git workspace. Each agent run is typically **~30–120 seconds** of focused work.

---

## How it works

```
You (CLI / Python / watcher)
        │
        ▼
  FleetDispatcher ── admission (max_parallel) ── worktree isolation
        │
        ├── persona: coder    ──► Cursor SDK session (Composer, scoped paths, MCPs)
        ├── persona: reviewer ──► diff review, typed verdict
        └── …
        │
        ▼
  JSON result + git branch/PR + structured logs (~/.hermes/fleet/runs/*.jsonl)
```

**Personas** are markdown prompts with optional path allowlists. **Pipelines** define phase order (`simple`, `code_review`, `full`). **Repo config** (`.agent-fleet.yaml`) adds verify commands, scope rules, and PR-loop behavior.

Default model: **`composer-2.5`** (override per persona in `fleet.yaml`).

---

## Quick start

**Prerequisites:** Python 3.14, [Cursor API key](https://cursor.com/dashboard/integrations), a git repo to target.

```bash
git clone https://github.com/Evan-Kim2028/agent-fleet.git
cd agent-fleet
pip install -e ".[dev]"    # or: uv sync --frozen --group dev

export CURSOR_API_KEY=your_key_here
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml   # default_backend: cursor, default_model: composer-2.5
```

First run (coder implements, reviewer checks):

```bash
agent-fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --pipeline code_review
```

Expect JSON with `phases.execute` and `phases.review`, `status: completed` or a typed failure (`scope_violation`, `verify_failed`, `review_changes_requested`). Verify personas: `agent-fleet personas`.

Optional repo scaffold: `agent-fleet init /path/to/your/repo` — see [docs/NEW-REPO.md](docs/NEW-REPO.md).

---

## Running agents in the background

These modes all run **locally** on your machine, each backed by Cursor SDK sessions.

| Mode | Command / API | What happens |
|------|----------------|--------------|
| **One-shot task** | `agent-fleet run "…" --workspace …` | Single agent job, exits with JSON |
| **Parallel batch** | `dispatch_tasks(...)` with multiple goals | Up to `max_parallel` concurrent Composer agents; same-repo tasks auto-isolate in git worktrees |
| **PR loop watcher** | `agent-fleet loop --workspace …` or `agent-fleet-pr-loop` | Polls open `fleet/*` PRs → fix review findings → wait for CI → optional squash merge ([systemd example](examples/agent-fleet-pr-loop.service)) |
| **Issue comment trigger** | `agent-fleet-watch` | Polls GitHub issues for `/agent --persona …` and dispatches the full pipeline |

Configure concurrency in `~/.hermes/coding_fleet/fleet.yaml`:

```yaml
default_backend: cursor
default_model: composer-2.5
max_parallel: 3            # concurrent local Cursor agents
max_redispatches: 1        # retry once on hard failure with handoff context
timeout_seconds: 900
```

Structured run logs: `~/.hermes/fleet/runs/<run-id>.jsonl` (dispatch, PR loop, watcher events).

Persistent sessions and MCP (Playwright, etc.): [docs/SESSIONS.md](docs/SESSIONS.md) · [docs/MCP.md](docs/MCP.md).

---

## Pipelines

| Pipeline | Phases | Typical use |
|----------|--------|-------------|
| `simple` | execute | Trivial, low-risk edits |
| `code_review` | execute → scope → verify → review (+ optional auto-fix) | Default for merge-bound work |
| `full` | PLAN → RESEARCH → … → VERIFY → REVIEW → TECH_LEAD? | Larger features, branch + PR creation |

`code_review` with `pr_loop.enabled` and `auto_push` can open/update PRs and run the merge lifecycle automatically. Outcomes: `completed`, `scope_violation`, `verify_failed`, `review_changes_requested`, `review_blocked`, `error`.

Details: [docs/QUICKSTART.md](docs/QUICKSTART.md).

---

## Repo integration (`.agent-fleet.yaml`)

| Field | Purpose |
|-------|---------|
| `default_persona` | Default when `--persona` omitted |
| `test_command` / `lint_command` | Verification after implement (full pipeline) |
| `persona_scope_allowlist` | Path prefixes per persona — **highest-leverage guardrail** |
| `use_worktree` | Isolated worktree per run |
| `pr_loop` | Local watcher: review fix → CI fix → merge |
| `code_review.auto_fix` | Re-dispatch on reviewer `request_changes` |

Examples: [`examples/repo.agent-fleet.yaml`](examples/repo.agent-fleet.yaml) · [`examples/repo-full.agent-fleet.yaml`](examples/repo-full.agent-fleet.yaml).

---

## Personas

Registry: `~/.hermes/coding_fleet/fleet.yaml` (override with `--config`).

Prompts ship in `agent_fleet/personas/*.md`; override with repo `personas/` or absolute paths. Repo `.agent-fleet.yaml` scope wins over global `allowed_paths` when both apply.

```yaml
# fleet.yaml excerpt
default_backend: cursor
default_model: composer-2.5
personas:
  backend:
    prompt: coder.md
    model: composer-2.5
    allowed_paths: ["api/", "src/"]
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
# Batch: pass multiple tasks via FleetDispatcher for parallel local agents
```

---

## PR loop (local watcher)

For repos with open fleet PRs:

```yaml
# .agent-fleet.yaml
pr_loop:
  enabled: true
  branch_prefixes: [fleet/]
  fix_persona: coder
  auto_merge: true
  max_fix_attempts: 2
```

1. GitHub Action posts PR analysis (`examples/github/pr-analyzer.yml`)
2. **Local watcher** (`agent-fleet loop` or `agent-fleet-pr-loop`) dispatches Cursor fix agents, waits for CI, merges

Requires `gh` auth and `CURSOR_API_KEY`.

---

## Effective use (short)

1. **One persona per domain** — backend, frontend, infra; scope each with `persona_scope_allowlist`.
2. **Default to `code_review`** for anything merge-bound.
3. **Small, file-specific goals** — pass paths and verify commands in `--context`.
4. **Parallelize independent work** — different packages/files; avoid two agents on the same file.
5. **Use `full` sparingly** — day-to-day fixes: `code_review` is enough.

**Anti-patterns:** one unrestricted `coder` on a monorepo; vague goals; parallel edits to the same file; skipping review on production paths.

---

## Optional: other backends and orchestrators

Agent Fleet is **Cursor-first**. These alternatives use the same personas, pipelines, and repo config — swap `default_backend` or add a plugin when you need them.

| Integration | When to use | Setup |
|-------------|-------------|-------|
| **[Kimi Code CLI](docs/KIMI.md)** | You prefer Kimi instead of Cursor for execution | `default_backend: kimi`, `KIMI_API_KEY`, `kimi-cli` on PATH |
| **[Hermes](integrations/hermes/)** | You already use Hermes as a chat orchestrator | `./scripts/deploy-hermes.sh` · enable `coding_fleet` toolset |

Neither is required for CLI, Python, watcher, or issue-dispatch workflows.

---

## v0.6.0 highlights

| Area | Summary |
|------|---------|
| **Python 3.14** | Required runtime; CI gates ruff, ty, pytest on `main` |
| **Unified logging** | `FleetLogger` + JSONL for dispatch, PR loop, and watchers |
| **Handoff redispatch** | Hard failures retry with curated context injected into the next attempt |
| **DRY helpers** | Shared `github_cli`, `create_fleet_session`, `require_backend_env` |
| **Phase graph** | `TECH_LEAD` / `DESIGN_REVIEW` gates wired in `LocalFleetRunner` |

Prior: MCP catalog, persistent sessions — [docs/MCP.md](docs/MCP.md) · [docs/SESSIONS.md](docs/SESSIONS.md) · [docs/REDISPATCH.md](docs/REDISPATCH.md).

---

## Development

```bash
uv sync --frozen --group dev
uv run pytest -q
uv run ruff check agent_fleet tests integrations
uv run ty check agent_fleet tests integrations
```

Release process: [docs/RELEASE.md](docs/RELEASE.md).
