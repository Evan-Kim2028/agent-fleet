# Kimi Code CLI backend (optional)

Agent Fleet can execute fleet runs through **Kimi Code CLI** (`kimi-cli` subprocess) instead of the default Cursor SDK backend. Personas, pipelines, and repo scope are unchanged — only the execution adapter differs.

## Kimi execution backend

Use this backend when your fleet should run via `kimi-cli` and a Kimi Code subscription (`KIMI_API_KEY`).

| Setting | Value |
|---------|-------|
| `default_backend` | `kimi` |
| API key | `KIMI_API_KEY` (often `sk-kimi-...`) |
| Default model | `kimi-for-coding` |
| Runtime | `kimi-cli` binary |

Personas, `code_review`, `.agent-fleet.yaml`, and batch dispatch work the same as with the default backend.

## Prerequisites

1. **Python 3.14** and agent-fleet installed:

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

## Fleet config

Copy the example config if you haven't already:

```bash
mkdir -p ~/.agent-fleet
cp examples/fleet.kimi.yaml ~/.agent-fleet/fleet.yaml
```

Or start from the default example and edit manually:

```bash
cp fleet.example.yaml ~/.agent-fleet/fleet.yaml
```

Edit `~/.agent-fleet/fleet.yaml` — set the Kimi backend:

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

When `default_backend: kimi`, the fleet uses `KIMI_API_KEY` — `CURSOR_API_KEY` is not required.

### Default backend (Cursor SDK)

To return to the default execution backend:

```yaml
default_backend: cursor
default_model: composer-2.5
```

And export `CURSOR_API_KEY`.

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

## Repo scope

`agent-fleet init /path/to/repo` and set scope in `.agent-fleet.yaml`:

```yaml
persona_scope_allowlist:
  backend:
    - src/
```

Scope is injected into the persona prompt at dispatch — same behavior regardless of backend.

## Gateway plugin (optional)

1. Deploy the plugin (pull + install + symlink + restart):

   ```bash
   ./scripts/deploy-hermes.sh
   ```

2. Set `default_backend: kimi` in `~/.agent-fleet/fleet.yaml`

3. Export `KIMI_API_KEY` in the gateway host environment (or your shell before dispatch)

4. Dispatch via `coding_fleet_dispatch` — backend comes from fleet config:

   ```json
   {
     "goal": "Fix failing test in src/foo.py",
     "workspace": "/absolute/path/to/repo",
     "persona": "coder",
     "pipeline": "code_review",
     "context": "Verify: pytest -q src/tests"
   }
   ```

## Python API

```python
from agent_fleet import dispatch_tasks

# Uses backend from ~/.agent-fleet/fleet.yaml (default_backend: kimi)
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
| `KIMI_API_KEY is not set` | `export KIMI_API_KEY=...` |
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

Both backends share the same adapter pattern; the standalone package ships both.

## See also

- [QUICKSTART.md](QUICKSTART.md) — general setup
- [PERSONAS.md](PERSONAS.md) — persona and scope configuration
- [../examples/fleet.kimi.yaml](../examples/fleet.kimi.yaml) — Kimi backend config template
