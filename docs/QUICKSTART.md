# Quickstart

Get from zero to a first fleet run in ~15 minutes.

Agent Fleet runs scoped personas through review pipelines against a git workspace. Pick an **execution backend** in `fleet.yaml` (Cursor SDK is the default; Kimi Code CLI is optional).

| Backend | Key | Setup section |
|---------|-----|---------------|
| **Cursor SDK** (default) | `CURSOR_API_KEY` | [§2 Default backend](#2-global-fleet-config) · [§3 First run](#3-first-run) |
| **Kimi Code CLI** (optional) | `KIMI_API_KEY` | [§2 Kimi backend](#kimi-backend-optional) · [§3b First run](#3b-first-run-kimi-backend-optional) · [KIMI.md](KIMI.md) |

## 1. Install

```bash
git clone https://github.com/Evan-Kim2028/agent-fleet.git
cd agent-fleet
pip install -e ".[dev]"
```

Requires Python 3.11+.

## 2. Global fleet config

```bash
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml
```

### Default backend (Cursor SDK)

```bash
export CURSOR_API_KEY=your_key_here   # https://cursor.com/dashboard/integrations
```

`fleet.yaml` should have:

```yaml
default_backend: cursor
default_model: composer-2.5
```

### Kimi backend (optional)

```bash
export KIMI_API_KEY=sk-kimi-...       # https://platform.kimi.ai
# kimi-cli must be on PATH
```

Edit `~/.hermes/coding_fleet/fleet.yaml`:

```yaml
default_backend: kimi
default_model: kimi-for-coding
```

See **[KIMI.md](KIMI.md)** for full Kimi Code CLI setup.

Verify personas load with either backend:

```bash
agent-fleet personas
# → coder, reviewer, explorer
```

## 3. First run

Pick any git repo and run:

```bash
agent-fleet run "Add a one-line comment to README explaining the project" \
  --workspace /path/to/your/repo \
  --pipeline code_review
```

Expected behavior:

- Takes ~30–120 seconds (fleet agents run in the repo)
- Prints JSON with `phases.execute`, optional `phases.scope` / `phases.verify`, and `phases.review`
- Final `status`: `completed`, `scope_violation`, `verify_failed`, `review_changes_requested`, or `review_blocked`

## 3b. First run (Kimi backend, optional)

Set `default_backend: kimi` in `fleet.yaml`, then use the same command:

```bash
export KIMI_API_KEY=sk-kimi-...

agent-fleet run "Add a one-line comment to README explaining the project" \
  --workspace /path/to/your/repo \
  --pipeline code_review
```

Expected behavior:

- Takes ~30–180 seconds (`kimi-cli` runs in the repo)
- Same JSON shape (`phases.execute`, `phases.review`)

Change backends anytime by editing `default_backend` in `fleet.yaml`.

## 4. Repo integration (recommended)

Scaffold per-repo settings:

```bash
agent-fleet init /path/to/your/repo
```

Edit `/path/to/your/repo/.agent-fleet.yaml`:

```yaml
name: my-project
default_persona: coder
test_command: pytest -q
lint_command: ruff check .

persona_scope_allowlist:
  backend:
    - src/
  frontend:
    - web/
```

Re-run with repo scope applied automatically:

```bash
agent-fleet run "Fix failing test in src/" \
  --workspace /path/to/your/repo \
  --persona backend \
  --pipeline code_review
```

## 5. Hermes (optional)

For Discord / Hermes orchestration, deploy the bundled plugin (pull + pip install + symlink + gateway restart):

```bash
./scripts/deploy-hermes.sh
```

First-time manual link (if you prefer):

```bash
ln -sf "$(pwd)/integrations/hermes" ~/.hermes/plugins/cursor-fleet
```

Add to `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [cursor-fleet]
toolsets:
  - coding_fleet
```

Put the API key for your chosen backend in `~/.hermes/.env`:

```bash
# Cursor SDK (default)
CURSOR_API_KEY=...

# Kimi backend (when default_backend: kimi in fleet.yaml)
KIMI_API_KEY=sk-kimi-...
```

Restart the gateway, then dispatch via `coding_fleet_dispatch` or `@hermes_lao` with persona + workspace + pipeline.

## 6. Batch parallel tasks

Use the Python API or Hermes `coding_fleet_dispatch` with a `tasks` array (see [PERSONAS.md](PERSONAS.md)):

```python
from agent_fleet import dispatch_tasks

results = dispatch_tasks(
    tasks=[
        {"goal": "Add tests for foo.py", "persona": "coder", "workspace": "/path/to/repo"},
        {"goal": "Update docs for foo", "persona": "coder", "workspace": "/path/to/repo"},
    ],
    pipeline="code_review",
)
```

The execution backend is read from `fleet.yaml` automatically.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `CURSOR_API_KEY is not set` | Export key or set `default_backend: kimi` |
| `KIMI_API_KEY is not set` | Export key or set `default_backend: cursor` — see [KIMI.md](KIMI.md) |
| `kimi-cli failed` | Install Kimi Code CLI; set `kimi_bin` in `fleet.yaml` |
| `agent-fleet: command not found` | Re-run `pip install -e ".[dev]"` |
| Persona not found | Run `agent-fleet personas`; check `fleet.yaml` |
| Agent edits wrong directories | Set `persona_scope_allowlist` in `.agent-fleet.yaml` |
| Parallel tasks overwrite each other | Parallel batch auto-creates one worktree + branch per task; or set `use_worktree: true` for single runs |

## Next

- [PERSONAS.md](PERSONAS.md) — customize personas and fleets
- [KIMI.md](KIMI.md) — Kimi Code CLI backend
- [../examples/repo.agent-fleet.yaml](../examples/repo.agent-fleet.yaml) — repo config template
- [../examples/silphco.agent-fleet.yaml](../examples/silphco.agent-fleet.yaml) — multi-persona monorepo
