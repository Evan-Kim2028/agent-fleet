# Kimi Code CLI backend (optional)

Use your **Kimi Code subscription** instead of Cursor API credits. Same agent fleet — personas, pipelines, repo scope, Hermes dispatch — different execution backend (`kimi-cli` subprocess).

## When to use Kimi vs Cursor

| Use Kimi | Use Cursor (default) |
|----------|----------------------|
| You have a Kimi Code CLI subscription | You have Cursor API / Composer access |
| You want `KIMI_API_KEY` billing | You want `CURSOR_API_KEY` billing |
| Silphco-style `kimi-cli` runs work for you | You want native Cursor SDK integration |

Everything else (personas, `code_review`, `.agent-fleet.yaml`, batch dispatch) is identical.

## Prerequisites

1. **Python 3.11+** and agent-fleet installed:

   ```bash
   git clone https://github.com/Evan-Kim2028/agent-fleet.git
   cd agent-fleet
   pip install -e ".[dev]"
   ```

2. **`kimi-cli` on PATH** — install from [Kimi Code](https://platform.kimi.ai) (Moonshot). Verify:

   ```bash
   kimi-cli --help
   # or: ~/.local/bin/kimi-cli --help
   ```

3. **Kimi Code API key** — from the Kimi Code console. Keys often start with `sk-kimi-`.

   ```bash
   export KIMI_API_KEY=sk-kimi-your_key_here
   ```

   For Hermes, add the same to `~/.hermes/.env`:

   ```bash
   KIMI_API_KEY=sk-kimi-your_key_here
   ```

## Fleet config

Copy the example config if you haven't already:

```bash
mkdir -p ~/.hermes/coding_fleet
cp fleet.example.yaml ~/.hermes/coding_fleet/fleet.yaml
```

Edit `~/.hermes/coding_fleet/fleet.yaml` — switch backend to Kimi:

```yaml
default_backend: kimi
default_model: kimi-for-coding
default_persona: coder
default_pipeline: code_review
timeout_seconds: 900

# Optional if kimi-cli is not on PATH:
# kimi_bin: /home/you/.local/bin/kimi-cli

personas:
  coder:
    prompt: coder.md
  reviewer:
    prompt: reviewer.md
  explorer:
    prompt: explorer.md
    mode: plan
```

**Do not set** `CURSOR_API_KEY` as required when running Kimi-only — the CLI checks the backend from `fleet.yaml`.

### Switch back to Cursor

```yaml
default_backend: cursor
default_model: composer-2.5
```

And export `CURSOR_API_KEY` instead.

## First run (CLI)

```bash
export KIMI_API_KEY=sk-kimi-...

agent-fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --pipeline code_review
```

Expected:

- ~30–180 seconds (kimi-cli runs in the repo with `--work-dir`)
- JSON with `phases.execute` and `phases.review`
- Reviewer verdict: `APPROVE` or `REQUEST_CHANGES`

Verify personas load:

```bash
agent-fleet personas
```

## Repo scope (same as Cursor)

`agent-fleet init /path/to/repo` and set scope in `.agent-fleet.yaml`:

```yaml
persona_scope_allowlist:
  backend:
    - src/
```

Scope applies to Kimi runs the same way — injected into the persona prompt at dispatch.

## Hermes / Discord

1. Symlink the plugin (if not already):

   ```bash
   ln -sf /path/to/agent-fleet/integrations/hermes ~/.hermes/plugins/cursor-fleet
   ```

2. Set `default_backend: kimi` in `~/.hermes/coding_fleet/fleet.yaml`

3. Add `KIMI_API_KEY` to `~/.hermes/.env`

4. Restart the Hermes gateway

5. Dispatch as usual — `coding_fleet_dispatch` reads backend from fleet config:

   ```json
   {
     "goal": "Fix failing test in src/foo.py",
     "workspace": "/absolute/path/to/repo",
     "persona": "coder",
     "pipeline": "code_review",
     "context": "Verify: pytest -q src/tests"
   }
   ```

Hermes orchestrator can stay on any model (e.g. glm); only the **fleet execution** uses Kimi.

## Python API

```python
from agent_fleet import dispatch_tasks

# Uses backend from ~/.hermes/coding_fleet/fleet.yaml (default_backend: kimi)
results = dispatch_tasks(
    goal="Fix login bug",
    workspace="/path/to/repo",
    pipeline="code_review",
)
```

Ensure `KIMI_API_KEY` is in the environment before calling.

## Per-persona model override

Optional — override the global Kimi model for one persona:

```yaml
default_backend: kimi
default_model: kimi-for-coding

personas:
  coder:
    prompt: coder.md
    model: kimi-for-coding
  explorer:
    prompt: explorer.md
    mode: plan
    model: kimi-for-coding
```

Persona `model` is passed through to the backend when supported.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `KIMI_API_KEY is not set` | `export KIMI_API_KEY=...` or add to `~/.hermes/.env` |
| `kimi-cli failed` / command not found | Install Kimi Code CLI; set `kimi_bin` in `fleet.yaml` |
| Still asks for `CURSOR_API_KEY` | Confirm `default_backend: kimi` in the fleet.yaml being loaded |
| Wrong backend loaded | Pass `--config /path/to/fleet.yaml` or set `CODING_FLEET_CONFIG` |
| Timeout | Raise `timeout_seconds` in `fleet.yaml` (default 900) |
| Agent edits wrong dirs | Set `persona_scope_allowlist` in `.agent-fleet.yaml` |

## How it works (implementation)

- `default_backend: kimi` → `KimiBackend` (`agent_fleet/kimi_backend.py`)
- Spawns `kimi-cli` with an isolated config pointing at `https://api.kimi.com/coding/v1`
- Uses model `kimi-for-coding` by default
- Implements the same `LLMBackend` protocol as `CursorBackend`

This is the same adapter pattern used in silphcoanalytics; the standalone package ships both backends.

## See also

- [QUICKSTART.md](QUICKSTART.md) — general setup
- [PERSONAS.md](PERSONAS.md) — persona and scope configuration
- [../examples/fleet.kimi.yaml](../examples/fleet.kimi.yaml) — Kimi backend config template
