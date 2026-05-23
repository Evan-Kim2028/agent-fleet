# Quickstart

Get from zero to a first fleet run in ~15 minutes.

## 1. Install

```bash
git clone <your-private-repo-url> agent_fleet
cd agent_fleet
pip install -e ".[dev]"
```

Requires Python 3.11+ and a [Cursor API key](https://cursor.com/dashboard/integrations).

```bash
export CURSOR_API_KEY=your_key_here
```

## 2. Global fleet config

```bash
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml
```

Verify:

```bash
agent-fleet personas
# → coder, reviewer, explorer
```

## 3. First run (CLI)

Pick any git repo and run:

```bash
agent-fleet run "Add a one-line comment to README explaining the project" \
  --workspace /path/to/your/repo \
  --pipeline code_review
```

Expected behavior:

- Takes ~30–120 seconds (Cursor Composer runs in the repo)
- Prints JSON with `phases.execute` and `phases.review`
- Reviewer verdict: `APPROVE` or `REQUEST_CHANGES`

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

For Discord / Hermes orchestration, symlink the bundled plugin:

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

Put `CURSOR_API_KEY` in `~/.hermes/.env`, restart the gateway, then dispatch via the `coding_fleet_dispatch` tool or `@hermes_lao` with persona + workspace + pipeline.

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

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `CURSOR_API_KEY is not set` | Export key or add to `~/.hermes/.env` |
| `agent-fleet: command not found` | Re-run `pip install -e ".[dev]"` |
| Persona not found | Run `agent-fleet personas`; check `fleet.yaml` |
| Agent edits wrong directories | Set `persona_scope_allowlist` in `.agent-fleet.yaml` |
| `init` fails on new directory | Fixed in 0.2.0 — upgrade and retry |
| Parallel tasks overwrite each other | Enable `use_worktree: true` in `.agent-fleet.yaml` |

## Next

- [PERSONAS.md](PERSONAS.md) — customize personas and fleets
- [../examples/repo.agent-fleet.yaml](../examples/repo.agent-fleet.yaml) — repo config template
- [../examples/silphco.agent-fleet.yaml](../examples/silphco.agent-fleet.yaml) — multi-persona monorepo
