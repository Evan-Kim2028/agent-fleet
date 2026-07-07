# OpenRouter backend (optional)

Agent Fleet can execute fleet runs through **OpenRouter** (OpenAI-compatible HTTP API) instead of the default Cursor SDK backend. Personas, pipelines, and repo scope are unchanged — only the execution adapter differs.

## OpenRouter execution backend

Use this backend when your fleet should run via OpenRouter's `/api/v1/chat/completions` endpoint and an OpenRouter API key (`OPENROUTER_API_KEY`).

| Setting | Value |
|---------|-------|
| `default_backend` | `openrouter` |
| API key | `OPENROUTER_API_KEY` (from openrouter.ai/keys) |
| Default model | `tencent/hy3:free` (295B MoE reasoning model) |
| Runtime | HTTP via stdlib `urllib.request` — no binary to install |

Personas, `code_review`, `.agent-fleet.yaml`, and batch dispatch work the same as with the default backend.

## Prerequisites

1. **Python 3.14** and agent-fleet installed:

   ```bash
   git clone https://github.com/Evan-Kim2028/agent-fleet.git
   cd agent-fleet
   pip install -e ".[dev]"
   ```

2. **OpenRouter API key** — create one at [openrouter.ai/keys](https://openrouter.ai/keys):

   ```bash
   export OPENROUTER_API_KEY=sk-or-your_key_here
   ```

   No binary installation is required — the backend uses only the Python standard library (`urllib.request`) for HTTP, mirroring the dependency-light approach of the GitHub integration.

## Fleet config

Copy the example config if you haven't already:

```bash
mkdir -p ~/.agent-fleet
cp examples/fleet.openrouter.yaml ~/.agent-fleet/fleet.yaml
```

Or start from the default example and edit manually:

```bash
cp fleet.example.yaml ~/.agent-fleet/fleet.yaml
```

Edit `~/.agent-fleet/fleet.yaml` — set the OpenRouter backend:

```yaml
default_backend: openrouter
# default_model is unset: the backend supplies its own default (tencent/hy3:free).
# To pin a different model, set it explicitly:
# default_model: anthropic/claude-3.5-sonnet
default_persona: coder
default_pipeline: code_review
timeout_seconds: 900
```

When `default_backend: openrouter`, the fleet uses `OPENROUTER_API_KEY` — `CURSOR_API_KEY` is not required, and `cursor_sdk` does not need to be installed.

### Default model behavior

If `default_model` is unset (or `null`), the OpenRouter backend defaults to `tencent/hy3:free`. Any OpenRouter model slug (`provider/model[:variant]`) can be pinned via `default_model` or per-persona `model`. Unlike the Cursor backend, there is no `fast` tier — the model string is passed through to OpenRouter unchanged.

### Switching backends

When switching backends, also switch `default_model` (or unset it to inherit the backend's default):

```yaml
# Cursor
default_backend: cursor
default_model: composer-2.5

# Kimi
default_backend: kimi
default_model: kimi-for-coding

# OpenRouter
default_backend: openrouter
# default_model: null  # inherits tencent/hy3:free
```

### Default backend (Cursor SDK)

To return to the default execution backend:

```yaml
default_backend: cursor
default_model: composer-2.5
```

And export `CURSOR_API_KEY`.

## First run (CLI)

```bash
export OPENROUTER_API_KEY=sk-or-...

agent-fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --pipeline code_review
```

Expected:

- 10–120 seconds (HTTP call to OpenRouter)
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

## Python API

```python
from agent_fleet import dispatch_tasks

# Uses backend from ~/.agent-fleet/fleet.yaml (default_backend: openrouter)
results = dispatch_tasks(
    goal="Fix login bug",
    workspace="/path/to/repo",
    pipeline="code_review",
)
```

Ensure `OPENROUTER_API_KEY` is in the environment before calling.

## Per-persona model override

Optional — override the global OpenRouter model for one persona:

```yaml
default_backend: openrouter
# default_model: null  # inherits tencent/hy3:free

personas:
  coder:
    prompt: coder.md
    model: tencent/hy3:free
  explorer:
    prompt: explorer.md
    mode: plan
    model: anthropic/claude-3.5-sonnet
```

Persona `model` is passed through to the backend.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `OPENROUTER_API_KEY is not set` | `export OPENROUTER_API_KEY=...` |
| `tencent/hy3:free` sunset | The free tier model may be retired; pin a different `default_model` |
| Still asks for `CURSOR_API_KEY` | Confirm `default_backend: openrouter` in the fleet.yaml being loaded |
| Wrong backend loaded | Pass `--config /path/to/fleet.yaml` or set `CODING_FLEET_CONFIG` |
| Timeout | Raise `timeout_seconds` in `fleet.yaml` (default 900) |
| Agent edits wrong dirs | Set `persona_scope_allowlist` in `.agent-fleet.yaml` |
| HTTP 429 rate limit | OpenRouter free tier has rate limits; pin a paid model or reduce `max_parallel` |

## How it works (implementation)

- `default_backend: openrouter` → `OpenRouterBackend` (`agent_fleet/openrouter_backend.py`)
- Sends POST to `https://openrouter.ai/api/v1/chat/completions` with `Authorization: Bearer <key>`
- Uses model `tencent/hy3:free` by default
- Implements the same `LLMBackend` protocol as `CursorBackend`
- No `create_session` — this is a non-session backend; `NoopSession` is used as the fallback

All three backends share the same adapter pattern and registry-driven factory; the standalone package ships all three. Backend modules are imported lazily — selecting `openrouter` never imports `cursor_backend` or `kimi_backend`.

## See also

- [QUICKSTART.md](QUICKSTART.md) — general setup
- [PERSONAS.md](PERSONAS.md) — persona and scope configuration
- [KIMI.md](KIMI.md) — Kimi Code CLI backend (alternative backend)
- [../examples/fleet.openrouter.yaml](../examples/fleet.openrouter.yaml) — OpenRouter backend config template
