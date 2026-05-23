# Agent Fleet

Standalone Python package for orchestrating **Composer/Cursor SDK** coding agents with pluggable personas and multi-phase pipelines.

**Docs:** [Quickstart](docs/QUICKSTART.md) · [Personas](docs/PERSONAS.md)

## Install

```bash
git clone <your-repo-url> agent_fleet
cd agent_fleet
pip install -e ".[dev]"
export CURSOR_API_KEY=...   # https://cursor.com/dashboard/integrations
```

Copy the example fleet config:

```bash
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml
```

## Quick start

```bash
# List personas
agent-fleet personas

# Run a task (requires CURSOR_API_KEY)
agent-fleet run "Add a health check" --workspace /path/to/repo --pipeline code_review

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
ln -sf /path/to/agent_fleet/integrations/hermes ~/.hermes/plugins/cursor-fleet
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
