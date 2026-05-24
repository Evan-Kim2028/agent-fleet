# Agent Fleet

A multi-agent coding orchestrator: scoped personas, review pipelines, and parallel dispatch — powered by **Cursor Composer** or **Kimi Code CLI**, from CLI, Python, or Hermes.

**Default backend:** Cursor SDK (`composer-2.5`). **Optional:** Kimi Code CLI subscription (`kimi-for-coding`) — same personas, pipelines, and repo scope.

**Docs:** [Quickstart](docs/QUICKSTART.md) · [New repo setup](docs/NEW-REPO.md) · [Fleet Scouts](docs/SCOUTS.md) · [Personas](docs/PERSONAS.md) · [Kimi backend](docs/KIMI.md) (optional)

## What Agent Fleet does

Define a **fleet** of personas (coder, reviewer, domain specialists), pick a **pipeline**, and dispatch against a git workspace. The orchestrator handles routing; fleet agents implement, verify, and review in scope.

| Capability | What you get |
|------------|--------------|
| **Scoped personas** | Path allowlists per persona so agents stay in `packages/foo/` instead of wandering the monorepo |
| **Review pipelines** | `code_review`: implement → **scope check** → **repo verify commands** → structured diff review with typed verdict |
| **Parallel dispatch** | Independent tasks (different files/packages) run concurrently up to `max_parallel` |
| **Repo factory config** | `.agent-fleet.yaml` per repo: verify commands, default persona, cross-cutting boundaries |
| **Orchestrator integration** | Hermes (or your app) plans and routes; fleet agents execute in the repo |
| **Full pipeline** | Larger tasks: plan → research → implement → **run your tests** → review (optional tech lead) |

Each dispatch is a non-interactive run (~30–120s). Best for focused, automatable tasks with clear goals and file-level context.

## Getting started

**Prerequisites:** Python 3.11+, [Cursor API key](https://cursor.com/dashboard/integrations), a git repo to target.

The default execution backend is Cursor SDK with `composer-2.5` in `fleet.example.yaml`. Override per-persona only when you need a different model.

```bash
# 1. Install
git clone https://github.com/Evan-Kim2028/agent-fleet.git
cd agent-fleet
pip install -e ".[dev]"

# 2. API key + fleet config (sets composer-2.5 default)
export CURSOR_API_KEY=your_key_here
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml

# 3. First run — coder implements, reviewer checks
agent-fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --pipeline code_review
```

You should see JSON output with `phases.execute` (coder) and `phases.review` (reviewer). Expect ~30–120 seconds.

Verify setup:

```bash
agent-fleet personas   # coder, reviewer, explorer
```

## Optional: Kimi Code CLI backend

Agent Fleet supports a second execution backend via `kimi-cli` and the [Kimi Code API](https://platform.kimi.ai). Personas, pipelines, repo scope, and Hermes dispatch are unchanged — set `default_backend` in `fleet.yaml`.

**Requires:** `kimi-cli` on PATH, `KIMI_API_KEY` (typically `sk-kimi-...`).

```bash
# Install kimi-cli (see Kimi Code docs), then:
export KIMI_API_KEY=your_kimi_code_key

# Point fleet config at the Kimi backend
cat >> ~/.hermes/coding_fleet/fleet.yaml <<'EOF'
default_backend: kimi
default_model: kimi-for-coding
EOF

# Same CLI — backend comes from fleet.yaml
agent-fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --pipeline code_review
```

Change backends by editing `default_backend` and the matching API key in your environment.

| Setting | Cursor SDK (default) | Kimi Code CLI |
|---------|----------------------|---------------|
| **Key** | `CURSOR_API_KEY` | `KIMI_API_KEY` |
| **Runtime** | `cursor-sdk` (pip) | `kimi-cli` |
| **Default model** | `composer-2.5` | `kimi-for-coding` |
| **Config** | `default_backend: cursor` | `default_backend: kimi` |

Both backends implement the same `LLMBackend` protocol — personas, pipelines, and `.agent-fleet.yaml` scope work the same way.

Full Kimi setup guide: **[docs/KIMI.md](docs/KIMI.md)**

Optional — scaffold repo integration:

```bash
agent-fleet init /absolute/path/to/your/repo
```

## Using it effectively

Think **dev factory**, not one mega-agent:

1. **One persona per domain** — e.g. `backend`, `frontend`, `infra`. Give each a markdown prompt with verify commands and anti-patterns.
2. **Scope every persona** — `persona_scope_allowlist` in `.agent-fleet.yaml` (or `allowed_paths` in `fleet.yaml`). This is the highest-leverage setting.
3. **Default to `code_review`** for anything merge-bound. Use `simple` only for trivial, low-risk edits.
4. **Small, file-specific goals** — pass paths and verify commands in `context`:

   ```bash
   agent-fleet run "Fix validation for nested structs" \
     --workspace /path/to/repo \
     --persona backend \
     --pipeline code_review \
     --context "File: src/models/user.py. Verify: pytest -q tests/test_user.py"
   ```

5. **Parallelize independent work** — batch via Python/Hermes when tasks touch different files (source + tests in different packages, or two packages). Parallel batch dispatch **auto-isolates** same-repo tasks in git worktrees (one branch per task). Set `use_worktree: true` in `.agent-fleet.yaml` to isolate single-task runs too.
6. **Orchestrator + fleet** — use Hermes (or scripts) for recon and routing; dispatch fleet agents for the actual edits. Don't make implementers re-discover the repo every time — put discovery in `context`.
7. **Bind skills to personas** — `skill: my-skill` in `fleet.yaml` injects `SKILL.md` conventions at dispatch (I/O rules, test commands, architecture).
8. **Use `full` pipeline sparingly** — for multi-step features where you want plan/research/verify loops and branch creation. Day-to-day fixes: `code_review` is enough.

**Anti-patterns:** one unrestricted `coder` on a monorepo; vague goals without file hints; parallel tasks editing the same file; skipping review on production paths.

## Install (reference)

```bash
git clone https://github.com/Evan-Kim2028/agent-fleet.git
cd agent-fleet
pip install -e ".[dev]"
export CURSOR_API_KEY=...   # https://cursor.com/dashboard/integrations
```

Copy the example fleet config (includes `default_model: composer-2.5`):

```bash
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml
```

## Quick start (reference)

```bash
agent-fleet run "Add a health check" \
  --workspace /path/to/repo \
  --pipeline code_review
```

```bash
# List personas
agent-fleet personas

# Scaffold repo integration
agent-fleet init /path/to/your/repo
```

## Integrate any repo

See **[docs/NEW-REPO.md](docs/NEW-REPO.md)** for the full checklist (local dispatch → GitHub PR analyzer → PR loop).

Drop `.agent-fleet.yaml` in your repo root (see `examples/repo.agent-fleet.yaml`):

| Field | Purpose |
|-------|---------|
| `default_persona` | Default agent when `--persona` omitted |
| `test_command` / `lint_command` | Post-implement verification (full pipeline) |
| `persona_scope_allowlist` | Path prefixes per persona (simple + full pipelines) |
| `cross_cutting_groups` | Planner decomposition boundaries |
| `critical_path_prefixes` | Protected paths (verify FATAL) |
| `use_worktree` | Isolated git worktree per run (also auto-enabled for parallel batch on the same repo) |

## Personas

Global registry: `~/.hermes/coding_fleet/fleet.yaml` (override with `--config`).

Persona bodies come from:

- Bundled `agent_fleet/personas/*.md` (ships with the package)
- Repo-local `personas/` via `.agent-fleet.yaml` → `personas_dir`
- Hermes skills via `skill:` key in `fleet.yaml`
- Absolute paths to any markdown file

Example custom persona in `fleet.yaml`:

```yaml
personas:
  backend:
    prompt: backend.md
    model: composer-2.5
    allowed_paths: ["api/", "src/"]
    extra_instructions: "Run pytest before finishing."
```

Repo scope (overrides `allowed_paths` when workspace has `.agent-fleet.yaml`):

```yaml
persona_scope_allowlist:
  backend:
    - api/
    - src/
```

## Pipelines

| Pipeline | Phases |
|----------|--------|
| `simple` | execute |
| `code_review` | execute → scope → verify (if configured) → review → **auto-fix?** |
| `full` | PLAN → RESEARCH → SYNTHESIZE → IMPLEMENT → VERIFY → REVIEW → TECH_LEAD? |

`code_review` runs mechanical scope checks on changed files, optional verify commands from `.agent-fleet.yaml`, then a diff-based reviewer that returns a typed verdict (`approve`, `request_changes`, `block`).

When `pr_loop.enabled: true` (or explicit `code_review.auto_fix: true`), the dispatcher **automatically re-dispatches a fix persona** on `request_changes` or `verify_failed`, then re-runs scope/verify/review. With `auto_push` + `auto_pr_loop`, it opens/updates the PR and runs the merge lifecycle.

| Outcome | Meaning |
|---------|---------|
| `completed` | All phases passed; reviewer approved |
| `scope_violation` | Implementer touched paths outside persona allowlist |
| `verify_failed` | Repo test/lint command failed |
| `review_changes_requested` | Reviewer returned `request_changes` |
| `review_blocked` | Reviewer returned `block` |
| `error` | Implementer or infrastructure failure |

`pipeline=full` is a special CLI/dispatch mode that runs the full orchestrator; other pipelines use the phase lists above.

## Hermes integration (optional)

Plugin source lives in this repo at `integrations/hermes/`. One command to pull, install, link, and restart:

```bash
./scripts/deploy-hermes.sh
```

Manual setup (first time only):

```bash
ln -sf "$(pwd)/integrations/hermes" ~/.hermes/plugins/cursor-fleet
```

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: [cursor-fleet]
toolsets:
  - coding_fleet
```

Set `CURSOR_API_KEY` in `~/.hermes/.env`.

## Python API

```python
from agent_fleet import dispatch_tasks

results = dispatch_tasks(
    goal="Fix login bug",
    workspace="/path/to/repo",
    pipeline="code_review",
)
```

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — 15-minute setup
- [docs/NEW-REPO.md](docs/NEW-REPO.md) — integrate any repo (scope, GHA, PR loop)
- [docs/PERSONAS.md](docs/PERSONAS.md) — persona fleet cookbook
- [docs/KIMI.md](docs/KIMI.md) — Kimi Code CLI backend (optional)

## PR loop (review → fix → CI → merge)

For repos with open `fleet/*` PRs, enable automated babysitting:

```yaml
# .agent-fleet.yaml
pr_loop:
  enabled: true
  branch_prefixes: [fleet/]
  poll_interval_s: 10          # watcher outer loop (default 10s)
  review_poll_s: 10            # wait for GHA review comment
  ci_poll_s: 10                # wait for CI checks to finish
  ci_register_poll_s: 5        # wait for checks to appear after push
  post_fix_poll_s: 15          # pause after fix push before re-checking CI
  fix_persona: coder          # review findings (workflows, config, code)
  ci_fix_persona: coder       # CI failures — use a persona scoped to fix CI/config paths
  auto_merge: true
  max_fix_attempts: 2
  max_ci_fix_attempts: 2
```

1. **GitHub Action** posts PR analysis (`agent-fleet-pr-analyzer` / `pr-analyzer.yml`)
2. **Local watcher** polls open fleet PRs, dispatches the fix persona for blocking findings, waits for CI, squash-merges to `main`

```bash
# One-shot poll (dry run friendly)
agent-fleet loop --workspace /path/to/repo --once

# Long-running watcher (systemd example: examples/agent-fleet-pr-loop.service)
agent-fleet-pr-loop --workspace /path/to/repo
```

Requires `gh` authenticated, `CURSOR_API_KEY` or `KIMI_API_KEY`, and `pr_review` configured.

## v0.5.0 highlights

Four new capabilities shipped in v0.5.0:

| Feature | Summary | Docs |
|---------|---------|------|
| **MCP catalog** | Declare Playwright, Chrome DevTools, Context7, and Serena in `fleet.yaml`; grant them per-persona via a named allowlist | [docs/MCP.md](docs/MCP.md) |
| **Persistent sessions** | All phases of a task share one Cursor agent ID and one MCP tool state instead of spawning a fresh agent per phase | [docs/SESSIONS.md](docs/SESSIONS.md) |
| **Hard-failure redispatch** | On `error`/`expired`/`timeout`/`scope_violation`, the dispatcher retries once with a fresh agent and a curated handoff note | [docs/REDISPATCH.md](docs/REDISPATCH.md) |
| **First-class MCP contracts** | `StdioMcpServerSpec` and `HttpMcpServerSpec` dataclasses mirror the Cursor SDK types and are validated at config-load time | [docs/MCP.md](docs/MCP.md) |

Design spec: [docs/superpowers/specs/2026-05-23-mcp-sessions-redispatch-design.md](docs/superpowers/specs/2026-05-23-mcp-sessions-redispatch-design.md)

## Development

```bash
pytest -q
ruff check agent_fleet tests
```
