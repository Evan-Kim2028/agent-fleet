# MCP Server Catalog

Model Context Protocol (MCP) servers extend agent capability by giving personas access to
external tools — a browser driver, a code-search index, API documentation, and so on. The
Cursor SDK forwards them directly to the agent as the `mcp_servers` kwarg on `Agent.create()`.

## What it does

MCP servers are declared once in a global `mcp_servers:` catalog at the top of your
`fleet.yaml`, then granted to individual personas via an `mcp_servers: [name, ...]` allowlist.
At dispatch time, the `CursorBackend.create_session()` factory resolves the allowlisted names
to their specs and passes them to the SDK. The SDK handles process lifecycle (stdio) and
authentication (HTTP/SSE); agent_fleet just wires the plumbing.

The per-persona allowlist mirrors how `allowed_paths` works: the catalog is global, personas
compose capabilities from it.

## Catalog format

```yaml
# fleet.yaml

mcp_servers:
  # --- stdio servers: spawn a subprocess, talk over stdin/stdout ---
  playwright:
    type: stdio
    command: npx
    args: ["-y", "@playwright/mcp@latest"]
    # env:          # optional extra environment for the subprocess
    #   DISPLAY: ":1"
    # cwd: /path    # optional working dir for the subprocess

  serena:
    type: stdio
    command: uvx
    args: ["--from", "git+https://github.com/oraios/serena", "serena-mcp-server"]

  # --- http/sse servers: HTTP endpoint, optional bearer auth ---
  context7:
    type: http
    url: https://mcp.context7.com/mcp
    headers:
      Authorization: "Bearer ${CONTEXT7_KEY}"
    # auth:                      # optional OAuth client-credentials flow
    #   client_id: your-client-id
    #   client_secret: your-client-secret
    #   scopes: [read, write]
```

Both `stdio` and `http`/`sse` types are supported. The `type` field defaults to `stdio` if
omitted.

### stdio spec fields

| Field | Required | Description |
|-------|----------|-------------|
| `command` | yes | Executable to spawn (resolved via `PATH`) |
| `args` | no | Command-line arguments, YAML list |
| `env` | no | Extra environment variables for the subprocess |
| `cwd` | no | Working directory for the subprocess |

### http/sse spec fields

| Field | Required | Description |
|-------|----------|-------------|
| `url` | yes | Full URL of the MCP endpoint |
| `headers` | no | HTTP headers (e.g. `Authorization`) |
| `auth.client_id` | no | OAuth2 client ID |
| `auth.client_secret` | no | OAuth2 client secret |
| `auth.scopes` | no | OAuth2 scopes list |

## Per-persona allowlist

```yaml
personas:
  coder:
    prompt: coder.md
    mcp_servers: [playwright, context7]

  reviewer:
    prompt: reviewer.md
    mcp_servers: [context7]

  product-scout:
    prompt: product-scout.md
    mode: plan
    mcp_servers: [playwright]
```

Names in `mcp_servers:` must match a top-level `mcp_servers:` key. Any unknown name causes
`load_fleet_config()` to raise at startup — you won't see the error mid-task:

```
ValueError: persona 'coder' references unknown MCP server 'playwrigth';
known: ['context7', 'playwright', 'serena']
```

A persona with an empty or absent `mcp_servers:` list just gets no MCPs — no warning, no
change in behavior from v0.4.

## Bundled recipes

Ready-to-paste YAML for the four supported MCPs:

```yaml
mcp_servers:
  playwright:
    type: stdio
    command: npx
    args: ["-y", "@playwright/mcp@latest"]

  chrome_devtools:
    type: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-chrome-devtools@latest"]
    # TODO: confirm exact npm package name for Chrome DevTools MCP

  context7:
    type: http
    url: https://mcp.context7.com/mcp
    headers:
      Authorization: "Bearer ${CONTEXT7_KEY}"

  serena:
    type: stdio
    command: uvx
    args: ["--from", "git+https://github.com/oraios/serena", "serena-mcp-server"]
```

Playwright and Serena are confirmed. The Chrome DevTools MCP package name is marked TODO —
verify on [npm](https://www.npmjs.com/search?q=chrome-devtools-mcp) before using.

## Env-var expansion

Any string value in the catalog (headers, env, command, args) supports `${VAR}` expansion.
Expansion happens at config-load time, not at dispatch time.

```yaml
mcp_servers:
  context7:
    type: http
    url: https://mcp.context7.com/mcp
    headers:
      Authorization: "Bearer ${CONTEXT7_KEY}"
      X-Trace-Id: "fleet-${RUN_ID}"
```

If `CONTEXT7_KEY` is not set in the environment when `load_fleet_config()` runs:

```
ValueError: environment variable 'CONTEXT7_KEY' required but not set
```

The error message contains the exact variable name. There is no default-value fallback
syntax (no `${VAR:-default}` support) — set the variable or remove the reference.

For local testing without a real key, set a placeholder:

```bash
export CONTEXT7_KEY=test-placeholder
agent-fleet run "..." --workspace /path/to/repo
```

## Smoke test

Requires `CURSOR_API_KEY` set, `cursor-sdk` installed (`pip install cursor-sdk`), and
`npx` on `PATH` (from Node.js ≥ 18).

```python
from pathlib import Path
from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.contracts.mcp import StdioMcpServerSpec

backend = CursorBackend()  # reads CURSOR_API_KEY from environment

session = backend.create_session(
    persona_name="smoke",
    cwd=Path.cwd(),
    mcp_servers={
        "playwright": StdioMcpServerSpec(
            command="npx",
            args=("-y", "@playwright/mcp@latest"),
        ),
    },
)

if session.agent_id is None and hasattr(session, "_message"):
    print("Session creation failed:", session._message)  # type: ignore[attr-defined]
else:
    result = session.send(
        "Use the playwright MCP to navigate to https://example.com and return the page title.",
        max_tokens=1024,
        timeout_s=120,
    )
    print("exit_code:", result.exit_code)
    print("stdout:", result.stdout[:500])
    session.dispose()
```

Expected: `exit_code: 0` and a page title in `stdout`. If `exit_code: 1`, check `result.stderr`
for the failure reason.

## Troubleshooting

**`npx` not on PATH.**
The stdio server process is spawned by the Cursor SDK, which inherits the environment of the
Python process. If `npx` isn't on `PATH` when you start Python, the SDK will fail to start
the server. Fix:

```bash
which npx   # should print a path
# if not: install Node.js (brew install node / apt install nodejs)
```

**HTTP MCP auth failures.**
Check that the `Authorization` header value is correct and the env var is set in the same
shell that starts Python. The SDK redacts secrets before sending them to Cursor Cloud VMs, so
you won't see them in logs. If `CONTEXT7_KEY` is wrong you'll see a non-zero status from the
first `send()` that uses a Context7 tool.

**Cursor SDK status mapping.**
The `CursorSession.send()` method maps SDK statuses to `exit_code`:

| SDK status | `exit_code` |
|------------|-------------|
| `finished` | 0 |
| `error` | 1 |
| `cancelled` | 1 |
| `expired` | 1 |

`exit_code=1` with `status=expired` mid-task is a hard failure and triggers the redispatch
loop (see [docs/REDISPATCH.md](REDISPATCH.md)).

**Serena first-run is slow.**
Serena LSP-indexes the workspace on first start. For large repos, allow 30–60 seconds before
the first tool call returns. The index is not cached across worktrees yet — each new worktree
cold-starts.
