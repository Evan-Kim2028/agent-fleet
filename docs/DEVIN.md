# Devin CLI backend (optional)

Agent Fleet can execute fleet runs through **Devin** (`devin -p`) instead of the default Cursor SDK backend. Personas, pipelines, and repo scope are unchanged — only the execution adapter differs.

## Devin execution backend

Use this backend when your fleet should run via the official Devin CLI with a **Devin Pro/Team** subscription. Auth is subscription OAuth via `devin auth login` — **no API key is required**.

| Setting | Value |
|---------|-------|
| `default_backend` | `devin` |
| Auth | `devin auth login` → `~/.local/share/devin/credentials.toml` (non-empty `windsurf_api_key`) |
| Default model | `swe-2-high` |
| Runtime | `devin` binary (`which devin` or `~/.local/bin/devin`) |
| Session support | Yes — `DevinSession` first send is fresh; later sends `-r <session_id>` |
| Session id capture | Parsed from the `--export <tmp>.json` file's top-level `session_id` field after each call (not `devin list`) |
| Retries | rate_limit / quota / transient / timeout are retried (default 4 retries, exponential backoff + jitter, capped at 120s); rate_limit/quota wait at least `DEVIN_RATE_LIMIT_COOLDOWN_S` (default 60s). A retry resumes the captured session instead of restarting. |
| Plan mode | `mode: plan` leaves `DEVIN_PERMISSION_MODE` unset (read-only auto-approve) instead of `bypass` |

Personas, `code_review`, `.agent-fleet.yaml`, and batch dispatch work the same as with the default backend.

## Prerequisites

1. **Python 3.14** and agent-fleet installed:

   ```bash
   git clone https://github.com/Evan-Kim2028/agent-fleet.git
   cd agent-fleet
   pip install -e ".[dev]"
   ```

2. **Devin CLI installed** and on `PATH` (or at `~/.local/bin/devin`).

3. **Subscription login**:

   ```bash
   devin auth login
   devin auth status   # sanity check
   ```

   Credentials are stored in `~/.local/share/devin/credentials.toml`. Fleet never injects a Devin API key.

## Fleet config

```bash
cp examples/fleet.devin.yaml ~/.agent-fleet/fleet.yaml
```

Or edit manually:

```yaml
default_backend: devin
default_model: swe-2-high
default_persona: coder
default_pipeline: code_review
timeout_seconds: 900

# Optional if devin is not on PATH:
# devin_bin: /home/you/.local/bin/devin
```

`AGENT_FLEET_BACKEND=devin` and `AGENT_FLEET_MODEL=swe-2-high` also work.

## Doctor

```bash
uv run agent-fleet doctor --backend devin
```

## Retry / rate-limit knobs

| Env | Default | Meaning |
|-----|---------|---------|
| `DEVIN_MAX_RETRIES` | `4` | Extra attempts after the first, for retryable failures |
| `DEVIN_RATE_LIMIT_COOLDOWN_S` | `60.0` | Minimum wait before retrying a rate-limit/quota failure |

## Notes

- Non-interactive mode can't show approval prompts, so agent-mode runs set `DEVIN_PERMISSION_MODE=bypass` per call; plan mode leaves it unset (default `auto` — read-only tools only, workspace edits are rejected).
- `--respect-workspace-trust false` is always passed (fleet worktrees are not pre-trusted).
- Prompts are written to a temp file and passed via `--prompt-file` to avoid argv length limits.
- Exhausted retries fail the phase with exit code 1; stderr names the failure classification (`rate_limit` / `quota` / `transient` / `timeout` / `error`).

## See also

- [OPENROUTER.md](OPENROUTER.md) — HTTP-only backend, no binary required
- [GROK.md](GROK.md) / [CMD.md](CMD.md) — other subscription-CLI backends with the same session-resume pattern
