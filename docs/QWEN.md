# Qwen backend (optional)

Agent Fleet can execute fleet runs through **Qwen** (Alibaba Bailian "Token Plan
Team Edition") instead of the default Cursor SDK backend. Personas, pipelines,
and repo scope are unchanged — only the execution adapter differs.

## Qwen execution backend

Use this backend when your fleet should run via Alibaba's OpenAI-compatible
Bailian endpoint and a Qwen API key (`QWEN_API_KEY`).

| Setting | Value |
|---------|-------|
| `default_backend` | `qwen` |
| API key | `QWEN_API_KEY` |
| Default model | `qwen3.8-max-preview` (hosted reasoning model, Token Plan Team Edition) |
| Base URL | `https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` |
| Runtime | HTTP via the shared OpenRouter OpenAI-compatible client — no binary to install |
| Tool calling | Yes — verified working OpenAI-style `tool_calls` |
| Reasoning effort | `OPENROUTER_REASONING_EFFORT` — `low` / `medium` / `high` / `none` (default `high`); the endpoint tolerates the `reasoning: {effort: ...}` field the shared client sends |
| Config knob | Optional `qwen_base_url` in `fleet.yaml` (defaults to the URL above) |

Personas, `code_review`, `.agent-fleet.yaml`, and batch dispatch work the same as
with the default backend.

## Prerequisites

1. **Python 3.14** and agent-fleet installed:

   ```bash
   git clone https://github.com/Evan-Kim2028/agent-fleet.git
   cd agent-fleet
   pip install -e ".[dev]"
   ```

2. **Qwen API key** — from your Alibaba Bailian Token Plan Team Edition account.
   Store it in the repo's gitignored `.env`:

   ```bash
   # .env
   QWEN_API_KEY=sk-...
   ```

   No binary installation is required — the backend reuses the existing
   OpenRouter OpenAI-compatible HTTP client (stdlib `urllib.request`).

## Fleet config

Copy the example config if you haven't already:

```bash
mkdir -p ~/.agent-fleet
cp examples/fleet.qwen.yaml ~/.agent-fleet/fleet.yaml
```

Or start from the default example and edit manually:

```bash
cp fleet.example.yaml ~/.agent-fleet/fleet.yaml
```

Edit `~/.agent-fleet/fleet.yaml` — set the Qwen backend:

```yaml
default_backend: qwen
default_model: qwen3.8-max-preview
default_persona: coder
default_pipeline: code_review
timeout_seconds: 900

# Optional — override the default Bailian endpoint:
# qwen_base_url: https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1

personas:
  coder:
    prompt: coder.md
  reviewer:
    prompt: reviewer.md
  explorer:
    prompt: explorer.md
    mode: plan
```

When `default_backend: qwen`, the fleet uses `QWEN_API_KEY` — `CURSOR_API_KEY`
is not required.

### Other models on the same endpoint

The same `QWEN_API_KEY` unlocks roughly 14 other models on the Bailian Token
Plan endpoint (e.g. `deepseek-v4-pro`, `deepseek-v4-flash`, `kimi-k2.7-code`,
`qwen3.7-max`). Point `default_model` (or a per-persona `model`) at any of
them — the `qwen` backend is not limited to `qwen3.8-max-preview`:

```yaml
default_backend: qwen
default_model: deepseek-v4-pro
```

### Default backend (Cursor SDK)

To return to the default execution backend:

```yaml
default_backend: cursor
default_model: composer-2.5
```

And export `CURSOR_API_KEY`.

## Switch to Qwen in one line

```bash
# Permanent for this machine
fleet config set-backend qwen

# Or session-wide (CLI + pr-analyzer + issue dispatch + pr_loop)
export AGENT_FLEET_BACKEND=qwen
export AGENT_FLEET_MODEL=qwen3.8-max-preview

# Or a single run / doctor check
fleet run "..." --backend qwen --pipeline code_review
fleet doctor --backend qwen
```

`AGENT_FLEET_BACKEND` is applied inside `load_fleet_config()` — every entry point
inherits it. No need to set it only for the PR analyzer.

## First run (CLI)

```bash
export QWEN_API_KEY=sk-...

fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --backend qwen \
  --pipeline code_review
```

Expected:

- 10–120 seconds (HTTP call to the Bailian endpoint)
- JSON with pipeline phases when using multi-phase pipelines
- Reviewer verdict: `APPROVE` or `REQUEST_CHANGES` (for `code_review`)

Verify personas load:

```bash
fleet personas
```

Check auth / environment:

```bash
fleet doctor --config examples/fleet.qwen.yaml
```

## Repo scope

`fleet init /path/to/repo` and set scope in `.agent-fleet.yaml`:

```yaml
persona_scope_allowlist:
  backend:
    - src/
```

Scope is injected into the persona prompt at dispatch — same behavior regardless of backend.

## Python API

```python
from agent_fleet import dispatch_tasks

# Uses backend from ~/.agent-fleet/fleet.yaml (default_backend: qwen)
results = dispatch_tasks(
    goal="Fix login bug",
    workspace="/path/to/repo",
    pipeline="code_review",
)
```

Ensure `QWEN_API_KEY` is in the environment before calling.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `401` / auth error | Confirm `QWEN_API_KEY` is set and valid; check it's loaded from `.env` |
| Still asks for `CURSOR_API_KEY` | Confirm `default_backend: qwen` in the fleet.yaml being loaded |
| Wrong backend loaded | Pass `--config /path/to/fleet.yaml` or set `CODING_FLEET_CONFIG` |
| Wrong endpoint / 404 | Check `qwen_base_url` matches the Bailian compatible-mode URL |
| Timeout | Raise `timeout_seconds` in `fleet.yaml` (default 900) |
| Agent edits wrong dirs | Set `persona_scope_allowlist` in `.agent-fleet.yaml` |

## How it works (implementation)

`default_backend: qwen` is a thin registration over the existing OpenRouter
OpenAI-compatible HTTP client — there is no separate `qwen_backend.py` module.
This works because the Bailian Token Plan endpoint speaks the same
OpenAI-compatible `/chat/completions` protocol as OpenRouter, including
tool-call responses (`tool_calls`) and tolerance of the `reasoning: {effort:
...}` field the shared client sends on every request. The registration simply
points the shared client at a different base URL
(`https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`,
overridable via `qwen_base_url`) and a different auth env var
(`QWEN_API_KEY`) and default model (`qwen3.8-max-preview`).

**Known wart:** `OPENROUTER_REASONING_EFFORT` still governs reasoning effort
for the `qwen` backend. The env var name says "openrouter", but it applies to
any backend built on the shared OpenAI-compatible client — including `qwen`.
There is no separate `QWEN_REASONING_EFFORT` variable.

## See also

- [OPENROUTER.md](OPENROUTER.md) — the shared OpenAI-compatible client that the Qwen backend reuses
- [GROK.md](GROK.md) — Grok Build CLI backend
- [PERSONAS.md](PERSONAS.md) — persona and scope configuration
- [../examples/fleet.qwen.yaml](../examples/fleet.qwen.yaml) — Qwen backend config template
