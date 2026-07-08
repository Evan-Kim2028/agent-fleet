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
| Tool calling | Yes — built-in file/shell tools (read_file, write_file, run_command, list_files) |
| Session support | Yes — `OpenRouterSession` maintains conversation history across phases |
| Reasoning effort | `OPENROUTER_REASONING_EFFORT` — `low` / `medium` / `high` / `none` (default `high`); sent to reasoning models |
| Tool-iteration cap | `OPENROUTER_MAX_TOOL_ITERATIONS` — integer bound on the tool-use loop (default `80`) |

Personas, `code_review`, `.agent-fleet.yaml`, and batch dispatch work the same as with the default backend. The backend implements `SessionCapableBackend` — when the fleet runner creates a session, `OpenRouterSession` drives a standard OpenAI-compatible tool-calling loop so the model can actually read files, write files, and run commands in the workspace. This mirrors what the Cursor SDK provides natively.

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
| HTTP 429 rate limit | Retried automatically (up to 3x with backoff, honoring `Retry-After`); if persistent, pin a paid model or reduce `max_parallel` |
| `no content, finish_reason=length` / reasoning exhaustion | `max_tokens` auto-escalates (doubling up to 65536) before failing; to spend less reasoning budget, lower `OPENROUTER_REASONING_EFFORT` (`medium`/`low`/`none`) |
| Run fails with `correction limit reached` | The model looped or hallucinated completion; the session fails loudly (exit 1) rather than accepting bad output — retry or pick a stronger model |
| Session hits `max tool iterations` | Raise `OPENROUTER_MAX_TOOL_ITERATIONS` (default 80) for very large tasks |

## How it works (implementation)

- `default_backend: openrouter` → `OpenRouterBackend` (`agent_fleet/openrouter_backend.py`)
- Sends POST to `https://openrouter.ai/api/v1/chat/completions` with `Authorization: Bearer <key>`
- Uses model `tencent/hy3:free` by default
- Implements both `LLMBackend` (stateless `run()`) and `SessionCapableBackend` (`create_session()`)
- **Session path:** `OpenRouterSession` drives a tool-calling loop — sends the prompt with `tools` (read_file, write_file, run_command, list_files), executes tool calls locally, feeds results back as `tool` role messages, and loops until `finish_reason: "stop"`. The loop is bounded by `OPENROUTER_MAX_TOOL_ITERATIONS` (default 80); hitting the cap fails the run with exit code 1. Conversation history persists across `send()` calls so the model retains context across phases.
- **Reliability guards:** the session detects repetition loops (a 50-char substring repeated 5+ times) and hallucinated completion claims made before any tool is called, and injects a corrective prompt. Up to 3 corrections are attempted; if the model still can't produce usable output, the run fails loudly with exit code 1 rather than silently accepting bad output.
- **Retries:** HTTP 429, 5xx responses, and transport errors are retried up to 3 times with exponential backoff (with jitter, capped at 30s), honoring the `Retry-After` header when present.
- **Bounded history:** once serialized conversation history exceeds ~400K chars, older `tool`-result bodies are elided (recent turns and the system prompt are preserved) so long sessions stay under the context limit even at high iteration caps.
- **Adaptive reasoning budget:** `OPENROUTER_REASONING_EFFORT` (default `high`, or `none` to omit) is sent to reasoning models. On reasoning exhaustion (reasoning content but no message content and `finish_reason: "length"`), `max_tokens` escalates — doubling up to 65536 — before failing. The escalated floor is sticky per session, so subsequent iterations start there instead of re-exhausting the low base budget every turn.
- **Stateless path:** `run()` sends a single prompt without tools — used by persona generation, PR analysis, and other non-coding callers.
- **Observability:** usage (input_tokens, output_tokens, cache_read_tokens) is normalized from OpenRouter's response and emitted to the fleet's `RunLog` — same as the Cursor backend.
- **Security:** file tools enforce path traversal protection (paths can't escape the workspace) and scope constraints from `allowed_tools` (`path:` prefixes restrict where `write_file` can create files). `run_command` is sandboxed to the workspace directory with a 60-second timeout, and when write scopes are configured it blocks obviously destructive invocations (`rm -rf` outside scope, `git clean`, `git reset --hard`). Tool execution is exception-safe: a handler that raises returns a recoverable JSON tool-error to the model instead of killing the session.

All three backends share the same adapter pattern and registry-driven factory; the standalone package ships all three. Backend modules are imported lazily — selecting `openrouter` never imports `cursor_backend` or `kimi_backend`.

## See also

- [QUICKSTART.md](QUICKSTART.md) — general setup
- [PERSONAS.md](PERSONAS.md) — persona and scope configuration
- [KIMI.md](KIMI.md) — Kimi Code CLI backend (alternative backend)
- [../examples/fleet.openrouter.yaml](../examples/fleet.openrouter.yaml) — OpenRouter backend config template
