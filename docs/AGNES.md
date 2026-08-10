# Agnes backend (optional)

Agent Fleet can execute fleet runs through **Agnes AI** (OpenAI-compatible
API at apihub.agnes-ai.com) instead of the default Cursor SDK backend.
Personas, pipelines, and repo scope are unchanged — only the execution adapter
differs.

## Agnes execution backend

Use this backend when your fleet should run via Agnes's OpenAI-compatible
endpoint and an Agnes API key (`AGNES_API_KEY`). Fleet orchestrates in Python;
no Grok parent tokens are required for routine runs.

| Setting | Value |
|---------|-------|
| `default_backend` | `agnes` |
| API key | `AGNES_API_KEY` |
| Default model | `agnes-2.5-flash` |
| Base URL | `https://apihub.agnes-ai.com/v1` |
| Runtime | HTTP via the shared OpenRouter OpenAI-compatible client — no binary to install |
| Tool calling | Yes — OpenAI-style `tool_calls` via the shared client |
| Config knob | Optional `agnes_base_url` in `fleet.yaml` (defaults to the URL above) |

**Free-tier note:** Agnes free tier is rate-limited (~20 RPM). Prefer
`max_parallel: 1` so fleet does not fan out concurrent model calls and trip
limits.

Personas, `code_review`, `.agent-fleet.yaml`, and batch dispatch work the same as
with the default backend.

## Prerequisites

1. **Python 3.14** and agent-fleet installed:

   ```bash
   git clone https://github.com/Evan-Kim2028/agent-fleet.git
   cd agent-fleet
   pip install -e ".[dev]"
   ```

2. **Agnes API key** — from your Agnes AI account (apihub.agnes-ai.com).
   Store it in the repo's gitignored `.env`:

   ```bash
   # .env
   AGNES_API_KEY=sk-...
   ```

   No binary installation is required — the backend reuses the existing
   OpenRouter OpenAI-compatible HTTP client (stdlib `urllib.request`).

## Fleet config

Copy the example config if you haven't already:

```bash
mkdir -p ~/.agent-fleet
cp examples/fleet.agnes.yaml ~/.agent-fleet/fleet.yaml
```

Or start from the default example and edit manually:

```bash
cp fleet.example.yaml ~/.agent-fleet/fleet.yaml
```

Edit `~/.agent-fleet/fleet.yaml` — set the Agnes backend:

```yaml
default_backend: agnes
default_model: agnes-2.5-flash
default_persona: coder
default_pipeline: code_review
timeout_seconds: 900
# Free tier ~20 RPM — keep concurrency low
max_parallel: 1

# Optional — override the default Agnes endpoint:
# agnes_base_url: https://apihub.agnes-ai.com/v1

personas:
  coder:
    prompt: coder.md
  reviewer:
    prompt: reviewer.md
  explorer:
    prompt: explorer.md
    mode: plan
```

When `default_backend: agnes`, the fleet uses `AGNES_API_KEY` — `CURSOR_API_KEY`
is not required.

### Default backend (Cursor SDK)

To return to the default execution backend:

```yaml
default_backend: cursor
default_model: composer-2.5
```

And export `CURSOR_API_KEY`.

## Switch to Agnes in one line

```bash
# Permanent for this machine
fleet config set-backend agnes

# Or session-wide (CLI + pr-analyzer + issue dispatch + pr_loop)
export AGENT_FLEET_BACKEND=agnes
export AGENT_FLEET_MODEL=agnes-2.5-flash

# Or a single run / doctor check
fleet run "..." --backend agnes --pipeline code_review
fleet doctor --backend agnes
```

`AGENT_FLEET_BACKEND` is applied inside `load_fleet_config()` — every entry point
inherits it. No need to set it only for the PR analyzer.

## First run (CLI)

```bash
export AGNES_API_KEY=sk-...

fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --backend agnes \
  --pipeline code_review
```

Expected:

- Seconds to minutes depending on task size (HTTP call to apihub.agnes-ai.com)
- JSON with pipeline phases when using multi-phase pipelines
- Reviewer verdict: `APPROVE` or `REQUEST_CHANGES` (for `code_review`)

Verify personas load:

```bash
fleet personas
```

Check auth / environment:

```bash
fleet doctor --config examples/fleet.agnes.yaml
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

# Uses backend from ~/.agent-fleet/fleet.yaml (default_backend: agnes)
results = dispatch_tasks(
    goal="Fix login bug",
    workspace="/path/to/repo",
    pipeline="code_review",
)
```

Ensure `AGNES_API_KEY` is in the environment before calling.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `401` / auth error | Confirm `AGNES_API_KEY` is set and valid; check it's loaded from `.env` |
| Still asks for `CURSOR_API_KEY` | Confirm `default_backend: agnes` in the fleet.yaml being loaded |
| Wrong backend loaded | Pass `--config /path/to/fleet.yaml` or set `CODING_FLEET_CONFIG` |
| Wrong endpoint / 404 | Check `agnes_base_url` matches `https://apihub.agnes-ai.com/v1` |
| Rate limit / 429 | Lower `max_parallel` to `1` (~20 RPM free tier); space out runs |
| Timeout | Raise `timeout_seconds` in `fleet.yaml` (default 900) |
| Agent edits wrong dirs | Set `persona_scope_allowlist` in `.agent-fleet.yaml` |

## How it works (implementation)

`default_backend: agnes` is a thin registration over the existing OpenRouter
OpenAI-compatible HTTP client — there is no separate `agnes_backend.py` module.
This works because the Agnes API hub speaks the same OpenAI-compatible
`/chat/completions` protocol as OpenRouter, including tool-call responses
(`tool_calls`). The registration simply points the shared client at a different
base URL (`https://apihub.agnes-ai.com/v1`, overridable via `agnes_base_url`)
and a different auth env var (`AGNES_API_KEY`) and default model
(`agnes-2.5-flash`).

**Known wart:** `OPENROUTER_REASONING_EFFORT` still governs reasoning effort
for the `agnes` backend when the shared client sends a `reasoning` field. The
env var name says "openrouter", but it applies to any backend built on the
shared OpenAI-compatible client — including `agnes`. There is no separate
`AGNES_REASONING_EFFORT` variable.

## See also

- [OPENROUTER.md](OPENROUTER.md) — the shared OpenAI-compatible client that the Agnes backend reuses
- [QWEN.md](QWEN.md) — similar thin registration pattern (Bailian)
- [GROK.md](GROK.md) — Grok Build CLI backend
- [PERSONAS.md](PERSONAS.md) — persona and scope configuration
- [../examples/fleet.agnes.yaml](../examples/fleet.agnes.yaml) — Agnes backend config template
