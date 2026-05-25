# Personas and fleet configuration

How to define, scope, and route coding agents.

## Two config layers

| Layer | File | Purpose |
|-------|------|---------|
| **Global fleet** | `~/.hermes/coding_fleet/fleet.yaml` | Personas, models, pipelines, parallelism |
| **Repo factory** | `.agent-fleet.yaml` in repo root | Verify commands, scope, worktrees, default persona |

Both merge at dispatch time. Repo settings override global defaults where noted below.

For persona loadouts, overlays, and local level-up journaling, see **[PERSONA-EVOLUTION.md](PERSONA-EVOLUTION.md)**.

## Execution backends

Agent Fleet runs the same personas and pipelines through a pluggable execution backend. Set `default_backend` in `fleet.yaml`:

```yaml
# Default — Cursor SDK, composer-2.5, CURSOR_API_KEY
default_backend: cursor
default_model: composer-2.5

# Optional — kimi-cli, Kimi Code subscription, KIMI_API_KEY
# default_backend: kimi
# default_model: kimi-for-coding
# kimi_bin: ~/.local/bin/kimi-cli
```

| Setting | Cursor SDK (default) | Kimi Code CLI |
|---------|----------------------|---------------|
| `default_backend` | `cursor` | `kimi` |
| API key env | `CURSOR_API_KEY` | `KIMI_API_KEY` |
| Default model | `composer-2.5` | `kimi-for-coding` |
| Runtime | `cursor-sdk` (pip) | `kimi-cli` binary |

Personas, pipelines, repo scope, and Hermes dispatch are **backend-agnostic**. Kimi setup: **[KIMI.md](KIMI.md)**.

## Global fleet (`fleet.yaml`)

Copy from `fleet.example.yaml`:

```yaml
default_model: composer-2.5
default_mode: agent
default_persona: coder
max_parallel: 3
timeout_seconds: 900
default_pipeline: simple

personas:
  coder:
    prompt: coder.md          # bundled in agent_fleet/personas/
    model: composer-2.5
    mode: agent
  reviewer:
    prompt: reviewer.md
  explorer:
    prompt: explorer.md
    mode: plan                # read-only planning

pipelines:
  simple:
    - execute
  code_review:
    - execute
    - review
```

### Persona body sources

1. **Bundled markdown** — `coder.md` resolves to `agent_fleet/personas/coder.md`
2. **Custom directory** — set `personas_dir: /path/to/personas`
3. **Hermes skill** — `skill: my-skill-name` (searches `~/.hermes/skills/`)
4. **Absolute path** — `prompt: /path/to/backend.md`

### Global scope (`allowed_paths`)

Optional per-persona path hints in `fleet.yaml`:

```yaml
personas:
  backend:
    prompt: backend.md
    allowed_paths:
      - src/
      - api/
```

Injected into the agent prompt as "only modify paths matching: …"

## Repo factory (`.agent-fleet.yaml`)

Generated via `agent-fleet init /path/to/repo`.

```yaml
name: my-app
default_persona: backend
default_branch: main

test_command: pytest -q
lint_command: ruff check .

personas_dir: agents/personas   # optional repo-local persona markdown

persona_scope_allowlist:
  backend:
    - src/
    - api/
  frontend:
    - web/
    - apps/web/

cross_cutting_groups:
  - [web/, api/]

critical_path_prefixes:
  - .github/workflows/

use_worktree: false
# worktree_base: /tmp/agent-fleet-worktrees
```

### Scope precedence

When dispatching against a workspace with `.agent-fleet.yaml`:

1. **`persona_scope_allowlist[persona]`** wins (repo-level, applies to `simple` and `code_review`)
2. Else **`allowed_paths`** from global `fleet.yaml`
3. Else unrestricted

This is wired in `YamlPersonaResolver.load()` when `repo_config` is merged at dispatch.

### Verify commands

Used by the **full** pipeline (`--pipeline full`):

- `test_command`, `lint_command`, `typecheck_command` → run after IMPLEMENT
- Or explicit `verify_commands: [...]` list

## Pipelines

| Name | CLI / Hermes | Phases |
|------|--------------|--------|
| `simple` | default | execute |
| `code_review` | recommended | execute → scope → verify? → review |
| `full` | `--pipeline full` | PLAN → RESEARCH → SYNTHESIZE → IMPLEMENT → VERIFY → REVIEW → TECH_LEAD? |

`full` is a special orchestrator mode (branch + worktree + verify loop). The `pipelines.full` list in yaml is documentation only for that path.

## Adding a new persona

### Step 1 — markdown body

Create `agents/personas/backend.md` in your repo (or add to global `personas_dir`):

```markdown
## Role

Backend engineer for this repo.

## Methodology

1. Read project conventions in CONTRIBUTING.md when present.
2. Run `pytest -q` (or the repo test command) before finishing.
3. Minimal diff — no unrelated refactors.

## Scope

Only edit `src/` and `api/` unless the task explicitly spans packages.
```

### Step 2 — register in fleet.yaml

```yaml
personas:
  backend:
    prompt: backend.md
    model: composer-2.5
```

If using repo-local `personas_dir`, the prompt filename is relative to that directory.

### Step 3 — scope in .agent-fleet.yaml

```yaml
persona_scope_allowlist:
  backend:
    - src/
    - api/
```

### Step 4 — verify

```bash
agent-fleet personas --workspace /path/to/repo
agent-fleet run "Add unit test for user validation" \
  --workspace /path/to/repo \
  --persona backend \
  --pipeline code_review
```

## Skill-backed personas

Bind a skill as the persona body:

```yaml
personas:
  lakehouse:
    skill: apache-lakehouse
    model: composer-2.5
```

Skill lookup order: `~/.hermes/skills/<name>/SKILL.md`, then repo `.cursor/skills/`.

## Hermes dispatch

Tool: `coding_fleet_dispatch`

```json
{
  "goal": "Implement issue #10 — fix user validation",
  "workspace": "/absolute/path/to/repo",
  "persona": "backend",
  "pipeline": "code_review",
  "context": "Primary file: src/models/user.py. Verify: pytest -q tests/test_user.py"
}
```

Batch (parallel, up to `max_parallel`):

```json
{
  "tasks": [
    {"goal": "Fix user.py validation", "persona": "backend", "workspace": "/path/to/repo"},
    {"goal": "Add tests", "persona": "backend", "workspace": "/path/to/repo"}
  ],
  "pipeline": "code_review"
}
```

**Safe batch rule:** parallel dispatch on the same repo auto-isolates each task in its own git worktree and branch. Completed runs keep the worktree path in the result (`worktree`, `branch_name`) for review/merge; failed runs tear down automatically.

## Routing cheat sheet (example monorepo)

| Path touched | Persona | Pipeline |
|--------------|---------|----------|
| `src/`, `api/` | `backend` | `code_review` |
| `web/`, `apps/web/` | `frontend` | `code_review` |
| `pipeline/`, `data/` | `data` | `code_review` |
| `infra/` | `infra` | `simple` or `code_review` |
| spans backend + frontend | batch 2 tasks | `code_review` |
| architecture / read-only | `explorer` | `simple` |

## Outcomes (full pipeline)

| `outcome` | Meaning |
|-----------|---------|
| `completed` | Verify OK, review approved |
| `review_blocked` | Reviewer returned `block` |
| `review_changes_requested` | Reviewer returned `request_changes` |
| `tech_lead_blocked` | Tech lead escalated/blocked |
| `verify_failed` | Tests/lint failed after retries |
| `decompose` | Task too cross-cutting — **auto-dispatches** child tasks (v0.6.4+) |
| `decompose_partial` | Some child tasks failed — see `DECOMPOSE_DISPATCH` in phases |
| `rejected` | Planner rejected the task |
