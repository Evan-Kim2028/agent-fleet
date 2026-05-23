# Agent Fleet

Orchestrate **Cursor Composer** or **Kimi Code CLI** as scoped coding agents: implement, review, and run in parallel — from CLI, Python, or Hermes.

**Default backend:** Cursor SDK (`composer-2.5`). **Optional:** Kimi Code CLI subscription (`kimi-for-coding`) — same personas, pipelines, and repo scope.

**Docs:** [Quickstart](docs/QUICKSTART.md) · [Personas](docs/PERSONAS.md)

## Why use this?

**Composer in Cursor IDE** is great for interactive editing. **Agent Fleet** is for when you want repeatable, automatable coding runs:

| Benefit | What you get |
|---------|----------------|
| **Scoped agents** | Personas + path allowlists so agents stay in `packages/foo/` instead of wandering the monorepo |
| **Built-in review** | `code_review` pipeline: one Composer implements, a second reviews — catches scope creep and missing tests |
| **Parallel dispatch** | Independent tasks (different files/packages) run concurrently up to `max_parallel` |
| **Repo factory config** | `.agent-fleet.yaml` per repo: verify commands, default persona, cross-cutting boundaries |
| **Orchestrator separation** | Hermes (or your app) plans and routes; Composer executes in the repo via API |
| **Full pipeline** | Larger tasks: plan → research → implement → **run your tests** → review (optional tech lead) |

**Tradeoffs (be honest):** each dispatch takes ~30–120s, uses Cursor API credits, and is non-interactive mid-run. Best for focused tasks with clear goals — not for exploratory back-and-forth in the IDE.

**When raw Composer / IDE is better:** ambiguous design, need to steer every edit, or UI/debugging workflows.

## Getting started (Composer 2.5)

**Prerequisites:** Python 3.11+, [Cursor API key](https://cursor.com/dashboard/integrations), a git repo to target.

Composer 2.5 is the default model in `fleet.example.yaml` (`default_model: composer-2.5`). You only need to override it per-persona if you want a different model.

```bash
# 1. Install
git clone https://github.com/Evan-Kim2028/agent-fleet.git
cd agent-fleet
pip install -e ".[dev]"

# 2. API key + fleet config (sets composer-2.5 default)
export CURSOR_API_KEY=your_key_here
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml

# 3. First run — Composer 2.5 implements + reviews
agent-fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --pipeline code_review
```

You should see JSON output with `phases.execute` (coder) and `phases.review` (reviewer). Expect ~30–120 seconds.

Verify setup:

```bash
agent-fleet personas   # coder, reviewer, explorer
```

## Optional: Kimi Code CLI (subscription)

Same fleet (personas, `code_review`, repo scope) — different execution backend. Uses `kimi-cli` against the [Kimi Code API](https://platform.kimi.ai) with your subscription key. This is the path we used in silphco before Cursor SDK was the default.

**Requires:** `kimi-cli` on PATH, `KIMI_API_KEY` (typically `sk-kimi-...`).

```bash
# Install kimi-cli (see Kimi Code docs), then:
export KIMI_API_KEY=your_kimi_code_key

# Switch fleet config to Kimi backend
cat >> ~/.hermes/coding_fleet/fleet.yaml <<'EOF'
default_backend: kimi
default_model: kimi-for-coding
EOF

# Same commands as Cursor — backend comes from fleet.yaml
agent-fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --pipeline code_review
```

Switch back anytime with `default_backend: cursor` and `CURSOR_API_KEY`.

| | Cursor (default) | Kimi (optional) |
|--|------------------|-----------------|
| **Key** | `CURSOR_API_KEY` | `KIMI_API_KEY` |
| **Binary/SDK** | `cursor-sdk` (pip) | `kimi-cli` (Kimi Code install) |
| **Default model** | `composer-2.5` | `kimi-for-coding` |
| **Billing** | Cursor API usage | Kimi Code subscription |

Personas, pipelines, `.agent-fleet.yaml` scope, and Hermes dispatch work identically — only the backend adapter changes (`LLMBackend` protocol).

Optional — scaffold repo integration:

```bash
agent-fleet init /absolute/path/to/your/repo
```

## Using it effectively

Think **dev factory**, not one mega-agent:

1. **One persona per domain** — e.g. `lakestore`, `frontend`, `infra`. Give each a markdown prompt with verify commands and anti-patterns.
2. **Scope every persona** — `persona_scope_allowlist` in `.agent-fleet.yaml` (or `allowed_paths` in `fleet.yaml`). This is the highest-leverage setting.
3. **Default to `code_review`** for anything merge-bound. Use `simple` only for trivial, low-risk edits.
4. **Small, file-specific goals** — pass paths and verify commands in `context`:

   ```bash
   agent-fleet run "Fix NameMapping for nested structs" \
     --workspace /path/to/repo \
     --persona lakestore \
     --pipeline code_review \
     --context "File: packages/lakestore/src/lakestore/_catalog.py. Verify: uv run pytest -q packages/lakestore/tests"
   ```

5. **Parallelize independent work** — batch via Python/Hermes when tasks touch different files (source + tests in different packages, or two packages). Enable `use_worktree: true` if they might overlap.
6. **Orchestrator + fleet** — use Hermes (or scripts) for recon and routing; dispatch Composer for the actual edits. Don't make the fleet agent re-discover the repo every time — put discovery in `context`.
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

Drop `.agent-fleet.yaml` in your repo root (see `examples/repo.agent-fleet.yaml`):

| Field | Purpose |
|-------|---------|
| `default_persona` | Default agent when `--persona` omitted |
| `test_command` / `lint_command` | Post-implement verification (full pipeline) |
| `persona_scope_allowlist` | Path prefixes per persona (simple + full pipelines) |
| `cross_cutting_groups` | Planner decomposition boundaries |
| `critical_path_prefixes` | Protected paths (verify FATAL) |
| `use_worktree` | Isolated git worktree per full-pipeline run |

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
| `code_review` | execute → review |
| `full` | PLAN → RESEARCH → SYNTHESIZE → IMPLEMENT → VERIFY → REVIEW → TECH_LEAD? |

`pipeline=full` is a special CLI/dispatch mode that runs the full orchestrator; other pipelines use the phase lists above.

## Hermes integration (optional)

Plugin source lives in this repo at `integrations/hermes/`. Symlink into Hermes:

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
- [docs/PERSONAS.md](docs/PERSONAS.md) — persona fleet cookbook

## Development

```bash
pytest -q
ruff check agent_fleet tests
```
