# Grok Build CLI backend (optional)

Agent Fleet can execute fleet runs through **Grok Build CLI** (`grok` subprocess) instead of the default Cursor SDK backend. Personas, pipelines, and repo scope are unchanged — only the execution adapter differs.

## Grok execution backend

Use this backend when your fleet should run via the official Grok Build CLI with a **SuperGrok / X Premium+** subscription. Auth is **subscription-only** via `grok login` — **no `XAI_API_KEY` is required**.

| Setting | Value |
|---------|-------|
| `default_backend` | `grok` |
| Auth | `grok login` → `~/.grok/auth.json` (OIDC / SuperGrok) |
| API key | **Not required** — do not set `XAI_API_KEY` for fleet runs |
| Default model | `grok-4.5` (Grok Build coding model; see `grok models`) |
| Runtime | `grok` binary (`which grok` or `~/.grok/bin/grok`) |
| Session support | Yes — `GrokSession` uses `-s` UUID on first send, `-r` on subsequent |

Personas, `code_review`, `.agent-fleet.yaml`, and batch dispatch work the same as with the default backend.

## Prerequisites

1. **Python 3.14** and agent-fleet installed:

   ```bash
   git clone https://github.com/Evan-Kim2028/agent-fleet.git
   cd agent-fleet
   pip install -e ".[dev]"
   ```

2. **Grok Build CLI on PATH** — install from [x.ai/cli](https://x.ai/cli):

   ```bash
   curl -fsSL https://x.ai/cli/install.sh | bash
   grok --help
   # or: ~/.grok/bin/grok --help
   ```

3. **Subscription login** (SuperGrok / X Premium+):

   ```bash
   grok login
   # headless: grok login --device-auth
   ```

   Credentials are stored in `~/.grok/auth.json`. Fleet never injects `XAI_API_KEY`.

## Fleet config

Copy the example config if you haven't already:

```bash
mkdir -p ~/.agent-fleet
cp examples/fleet.grok.yaml ~/.agent-fleet/fleet.yaml
```

Or start from the default example and edit manually:

```bash
cp fleet.example.yaml ~/.agent-fleet/fleet.yaml
```

Edit `~/.agent-fleet/fleet.yaml` — set the Grok backend:

```yaml
default_backend: grok
default_model: grok-4.5
default_persona: coder
default_pipeline: code_review
timeout_seconds: 900

# Optional if grok is not on PATH:
# grok_bin: /home/you/.grok/bin/grok

personas:
  coder:
    prompt: coder.md
  reviewer:
    prompt: reviewer.md
  explorer:
    prompt: explorer.md
    mode: plan
```

When `default_backend: grok`, the fleet uses subscription auth from `~/.grok/auth.json` — `CURSOR_API_KEY` and `XAI_API_KEY` are not required.

### Default backend (Cursor SDK)

To return to the default execution backend:

```yaml
default_backend: cursor
default_model: composer-2.5
```

And export `CURSOR_API_KEY`.

## Switch to Grok in one line

```bash
# Permanent for this machine
fleet config set-backend grok

# Or session-wide (CLI + pr-analyzer + issue dispatch + pr_loop)
export AGENT_FLEET_BACKEND=grok
export AGENT_FLEET_MODEL=grok-4.5

# Or a single run / doctor check
fleet run "..." --backend grok --pipeline code_review
fleet doctor --backend grok
```

`AGENT_FLEET_BACKEND` is applied inside `load_fleet_config()` — every entry point
inherits it. No need to set it only for the PR analyzer.

## First run (CLI)

```bash
# Ensure you are logged in (no XAI_API_KEY needed)
grok login

fleet run "Add a one-line project description to README" \
  --workspace /absolute/path/to/your/repo \
  --backend grok \
  --pipeline code_review
```

Expected:

- Headless `grok` runs in the repo with `--cwd` and `--yolo` (or `--permission-mode plan` for plan personas)
- JSON with pipeline phases when using multi-phase pipelines
- Reviewer verdict: `APPROVE` or `REQUEST_CHANGES` (for `code_review`)

Verify personas load:

```bash
fleet personas
```

Check auth / environment:

```bash
fleet doctor --config examples/fleet.grok.yaml
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

# Uses backend from ~/.agent-fleet/fleet.yaml (default_backend: grok)
results = dispatch_tasks(
    goal="Fix login bug",
    workspace="/path/to/repo",
    pipeline="code_review",
)
```

Ensure `grok login` has populated `~/.grok/auth.json` before calling.

## Per-persona model override

Optional — override the global Grok model for one persona:

```yaml
default_backend: grok
default_model: grok-4.5

personas:
  coder:
    prompt: coder.md
    model: grok-4.5
  explorer:
    prompt: explorer.md
    mode: plan
    model: grok-4.5
```

Persona `model` is passed through to the backend when supported. Plan mode maps to `grok --permission-mode plan`.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `grok binary not found` | Install from https://x.ai/cli; set `grok_bin` in `fleet.yaml` |
| `~/.grok/auth.json missing` / empty / invalid | Run `grok login` (or `grok login --device-auth`) |
| Still asks for `CURSOR_API_KEY` | Confirm `default_backend: grok` in the fleet.yaml being loaded |
| Wrong backend loaded | Pass `--config /path/to/fleet.yaml` or set `CODING_FLEET_CONFIG` |
| Timeout | Raise `timeout_seconds` in `fleet.yaml` (default 900) |
| Agent edits wrong dirs | Set `persona_scope_allowlist` in `.agent-fleet.yaml` |

## How it works (implementation)

- `default_backend: grok` → `GrokBackend` (`agent_fleet/grok_backend.py`)
- Spawns `grok` headless with `--prompt-file`, `--output-format plain`, `-m grok-4.5`
- Agent mode uses `--yolo`; plan mode uses `--permission-mode plan`
- Sessions: first send `-s <uuid>`, subsequent sends `-r <uuid>`
- Auth probe: binary present + non-empty valid JSON dict at `~/.grok/auth.json`
- Implements the same `LLMBackend` / `SessionCapableBackend` protocols as other backends


## GitHub PR analyzer

`agent-fleet-pr-analyzer` (GitHub Actions / `fleet` PR workflow) uses the same
backend resolution as `fleet run`:

1. Load `AGENT_FLEET_CONFIG` / `CODING_FLEET_CONFIG` / `~/.agent-fleet/fleet.yaml`
2. Optional override only if set: `AGENT_FLEET_BACKEND`, `AGENT_FLEET_MODEL`
3. Auth via `require_backend_env` — for Grok this is `grok login` / `~/.grok/auth.json`, **not** `CURSOR_API_KEY`
4. `make_backend(fleet_config)`

When `default_backend: grok` on the runner:

- Comment title defaults to **Grok PR Analysis** (unless `pr_review.comment_title` is customized)
- Footer labels the product as **Grok Build**
- No `AGENT_FLEET_BACKEND=cursor` in the workflow — leave backend unset so fleet.yaml wins
- Self-hosted runners need `grok` on PATH and a completed `grok login`

The pr_loop merge gate finds analyzer comments by any backend title or the
stable `**Risk Level:**` line in the formatted comment body.

## See also

- [QUICKSTART.md](QUICKSTART.md) — general setup
- [PERSONAS.md](PERSONAS.md) — persona and scope configuration
- [../examples/fleet.grok.yaml](../examples/fleet.grok.yaml) — Grok backend config template

## Autonomy control plane (PR loop)

PR loop merge/fix policy is centralized in `agent_fleet.autonomy.decide`.
Evidence (review, CI, paths, `review_addressed_for_sha`) maps to an `Action`
(`WAIT_REVIEW` / `FIX_REVIEW` / `FIX_CI` / `PARK` / `MERGE` / `NOOP`). See
[ADR 0002](adr/0002-autonomy-control-plane.md). Toggle with
`pr_loop.use_autonomy_decide` (default `true`).

