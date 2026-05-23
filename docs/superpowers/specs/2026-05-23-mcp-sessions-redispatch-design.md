# MCP Support, Persistent Task Sessions, and Outer Redispatch

**Date:** 2026-05-23
**Status:** Draft for review
**Target version:** agent_fleet v0.5.0

## Goal

Raise the per-task autonomy ceiling of `agent_fleet` so a single dispatched
task can drive longer, richer sessions and recover from hard failures without
human intervention. Four coordinated changes, all landed upstream in this
repo:

1. **MCP wiring** — surface the Cursor Python SDK's `mcp_servers` kwarg through
   the backend and config so personas can be granted Playwright,
   Chrome DevTools, Context7, and Serena tools.
2. **Persistent agent per task** — replace one-shot `Agent.prompt()` calls
   with a `Agent.create()` + repeated `send()` pattern, so planner →
   researcher → implementer → reviewer share one durable conversation and one
   MCP tool state across phases.
3. **Outer redispatch on hard failure** — add a dispatcher-level retry that
   reacts only to hard failures (error/cancelled/expired/timeout/scope
   violation/pipeline non-zero exit), spawns a *fresh* agent with a curated
   structured handoff from the failed attempt, and is capped by a small
   configurable budget.
4. **First-class MCP catalog** — Playwright (`@playwright/mcp`), Chrome
   DevTools MCP, Context7 (HTTP), and Serena (stdio LSP) become named entries
   in the fleet config that personas opt into via allowlist.

Non-goals: backend switch (Cursor stays primary), cross-repo coordination,
semantic memory across tasks, recursive task decomposition.

## Background

Today's surface (v0.4.2):

- `cursor_backend.py` calls `Agent.prompt()` once per phase. No
  `mcp_servers` is passed; tools are injected as prose hints via the
  `allowed_tools: ["path:..."]` scope note (`cursor_backend.py:73-84`).
- Each phase is a fresh agent — planner output is concatenated into a
  researcher prompt, researcher notes into a synthesizer prompt, etc. No
  shared model state, no tool state, no streaming MCP context.
- Retry budgets exist *inside* a task: `max_verify_retries=3`,
  `max_fix_attempts=2`. There is no retry layer *above* the task — a
  Cursor `status=error` or `expired` returns to the caller as a failed
  dispatch with a preserved worktree (`runner.py:57`).

The Cursor Python SDK already supports everything we need:

```python
Agent.create(
    model="composer-2.5",
    api_key=...,
    local=LocalAgentOptions(cwd=...),
    mcp_servers={  # <-- already supported
        "playwright": StdioMcpServerConfig(command="npx", args=["-y", "@playwright/mcp@latest"]),
        ...
    },
)
# returns durable handle with agent_id; supports .send() across many turns
```

Per the [Cursor Python SDK docs](https://cursor.com/docs/sdk/python),
`mcp_servers` is a first-class kwarg on `AgentOptions` and
`Agent.create()`, accepting `StdioMcpServerConfig`, `HttpMcpServerConfig`,
and `SseMcpServerConfig`. Sensitive auth fields are redacted before cloud
VMs see them. This is the lever the whole design pulls on.

## Architecture

### Component overview

```
.agent-fleet.yaml
├── mcp_servers:          # NEW: catalog of named MCP configs
│   playwright: { type: stdio, command: npx, args: [...] }
│   chrome_devtools: { type: stdio, command: npx, args: [...] }
│   context7: { type: http, url: ..., headers: { Authorization: ... } }
│   serena: { type: stdio, command: serena, args: [...] }
└── personas:
    coder:
      mcp_servers: [playwright, chrome_devtools, serena, context7]   # NEW: allowlist
      allowed_tools: [path:src/**]                                   # unchanged

agent_fleet/
├── sessions.py           # NEW: AgentSession protocol + CursorSession impl
├── cursor_backend.py     # MODIFIED: accepts mcp_servers + session_handle, stays stateless
├── kimi_backend.py       # MODIFIED: no-op session implementation
├── runner.py             # MODIFIED: opens one AgentSession per task, threads it through phases
├── dispatcher.py         # MODIFIED: wraps run_pipeline in dispatch_with_retry
├── redispatch.py         # NEW: handoff extraction + retry loop
├── config.py             # MODIFIED: loads + validates mcp_servers and persona allowlists
└── contracts/
    └── mcp.py            # NEW: McpServerSpec dataclass mirroring SDK types
```

### Component contracts

**`AgentSession` protocol (`sessions.py`).** One handle per task. Constructed
by `runner.py` after the worktree is provisioned, disposed when the task
ends (success, failure, or before redispatch).

```python
class AgentSession(Protocol):
    agent_id: str | None  # may be None for stateless backends

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
    ) -> CursorLLMResult: ...

    def dispose(self) -> None: ...
```

**`CursorSession` implementation.** Holds the Cursor `Agent` handle returned
by `Agent.create()` and forwards `send()` to `agent.send()`. Constructed
with the resolved `mcp_servers` dict for the persona. Catches Cursor SDK
errors and surfaces them with the same `exit_code != 0` convention
`CursorBackend.run()` already uses, so callers don't have to special-case.

**`NoopSession` for `kimi_backend.py`.** Each `send()` does a one-shot CLI
invocation (today's behavior). MCPs are not supported on kimi yet — the
session ignores `mcp_servers` and logs a warning if any are configured for
a persona that gets routed to kimi.

**`cursor_backend.py` stays stateless.** It exposes a `create_session(
persona, cwd, mcp_servers, model, mode) -> CursorSession` factory. The
old `run()` method stays for backward compat (callers that haven't
migrated still work, just without session persistence) and internally
becomes `session = create_session(...); result = session.send(prompt);
session.dispose()`.

**`config.py` validates the catalog + allowlist.** At load time:

- Parse top-level `mcp_servers:` into `dict[str, McpServerSpec]`.
- For each persona, resolve `mcp_servers: [name, ...]` against the catalog;
  fail loudly on unknown names.
- Env-var expansion in `headers` and `env` fields (e.g.
  `${CONTEXT7_KEY}`).

**`redispatch.py` handles outer retry.**

```python
def dispatch_with_retry(
    task: TaskSpec,
    *,
    max_redispatches: int = 1,
    on_event: Callable[[Event], None] | None = None,
) -> TaskResult:
    handoff: HandoffNote | None = None
    for attempt in range(max_redispatches + 1):
        result = dispatch(task, handoff=handoff)
        if not _is_hard_failure(result):
            return result
        handoff = _extract_handoff(result, previous=handoff)
    return result
```

`_is_hard_failure()` returns True for: Cursor `status in {error, cancelled,
expired}`, timeout exceeded, scope violation, pipeline exit ≠ 0. **Soft
failures (verify exhausted, reviewer rejected) do NOT trigger redispatch.**

`_extract_handoff()` produces a structured `HandoffNote` containing: the
failure mode, the verify/lint stderr if any, the list of files the failed
attempt modified (before revert), and a one-paragraph LLM-generated
"what was attempted, what NOT to repeat." Fresh attempts get this note
prepended to the planner prompt (planner sees it as "previous attempt
context — avoid these dead ends").

### Data flow

```
dispatch_with_retry(task)
  └─ attempt 0: dispatch(task, handoff=None)
       └─ runner.run_task(task)
            └─ session = backend.create_session(persona, cwd, mcp_servers)
                 └─ Cursor SDK: Agent.create(mcp_servers={...}) → agent_id=A1
            ├─ session.send(planner_prompt)        # A1, turn 1
            ├─ session.send(researcher_prompt)     # A1, turn 2 — MCPs (Serena, Context7) usable
            ├─ session.send(implementer_prompt)    # A1, turn 3 — MCPs (Playwright) usable
            ├─ session.send(verify_prompt) [×3 if needed]
            ├─ session.send(reviewer_prompt)
            └─ session.dispose()
       └─ result.status = "error" (e.g., Cursor expired mid-implementer)
  └─ _is_hard_failure(result) == True
  └─ handoff = _extract_handoff(result)            # files touched, stderr, LLM summary
  └─ attempt 1: dispatch(task, handoff=handoff)
       └─ session2 = backend.create_session(...) → agent_id=A2 (fresh)
       └─ planner sees handoff note prepended
       └─ ...
```

### Configuration example

`.agent-fleet.yaml`:

```yaml
mcp_servers:
  playwright:
    type: stdio
    command: npx
    args: ["-y", "@playwright/mcp@latest"]
  chrome_devtools:
    type: stdio
    command: npx
    args: ["-y", "chrome-devtools-mcp@latest"]
  context7:
    type: http
    url: https://mcp.context7.com/mcp
    headers:
      Authorization: "Bearer ${CONTEXT7_KEY}"
  serena:
    type: stdio
    command: uvx
    args: ["--from", "git+https://github.com/oraios/serena", "serena-mcp-server"]

redispatch:
  max_attempts: 1            # 0 disables; >1 allows multi-redispatch
  triggers: [error, cancelled, expired, timeout, scope_violation, pipeline_nonzero]

personas:
  coder:
    mcp_servers: [playwright, chrome_devtools, serena, context7]
    allowed_tools: [path:src/**, path:tests/**]
  reviewer:
    mcp_servers: [serena, context7]
    allowed_tools: [path:**]
  product_scout:
    mcp_servers: [playwright, chrome_devtools]
    allowed_tools: []
```

### Error handling

| Failure | Behavior |
|---|---|
| MCP server fails to start (stdio command not on PATH, http auth rejected) | Cursor SDK raises; `CursorSession.create_session` catches → returns `exit_code=1` with stderr identifying the server. No retry — config issue. |
| Cursor `Agent.create()` itself fails (auth, quota) | Hard failure, propagates to `dispatch_with_retry`. Redispatch will retry once (same auth/quota likely fails again — acceptable noise). |
| `send()` returns `status=expired` mid-task | Hard failure. Session disposed. Handoff extracted (files modified up to that point captured from worktree). Redispatch fresh. |
| Verify/reviewer rejects N times | Soft failure. NOT redispatched. Returns to caller as today (`outcome=verify_failed` / `outcome=review_rejected`). |
| MCP allowlisted for persona but unknown to catalog | Config load fails fast at fleet startup. |
| `mcp_servers` set on persona routed to kimi backend | `NoopSession` warns once and continues without MCPs. |
| Env var missing during catalog load (`${CONTEXT7_KEY}` unset) | Config load fails fast with clear "missing env var" message. |

### Testing strategy

- **Unit, `sessions.py`:** `CursorSession.send()` and `dispose()` against a
  fake SDK Agent (monkey-patched). Verify `mcp_servers` is forwarded
  verbatim. Verify failure modes map to `exit_code != 0`.
- **Unit, `redispatch.py`:** `_is_hard_failure()` table-driven test over
  every status/outcome combination. `_extract_handoff()` produces a
  `HandoffNote` with expected fields from a fixture failed-task result.
- **Unit, `config.py`:** allowlist resolution rejects unknown MCP names;
  env var expansion works; kimi-routed persona with MCPs warns.
- **Integration, in-repo:** A new `tests/integration/test_full_task_session.py`
  that runs a `simple`-pipeline task end-to-end against a recorded Cursor
  cassette (via `vcr.py` or a hand-rolled fixture), asserting one
  `Agent.create()` and multiple `send()` calls per task.
- **Integration, redispatch:** Fault-inject `status=expired` on the 3rd
  `send()`; assert `dispatch_with_retry` runs attempt 1 with the handoff,
  and that two distinct `agent_id`s are seen.

No live-MCP integration test in CI (network/cost). Document a manual
smoke test in `docs/MCP-SMOKE.md` for verifying each catalog entry locally
before a release.

## Open decisions (committed to defaults; flag on review)

These were not explicitly clarified during brainstorming; I'm going with
my read of the existing code. Push back on review if any are wrong.

1. **Session lifecycle owner = `runner.py`**, not `dispatcher.py` or the
   backend. Rationale: dispatcher owns worktree + branch; runner owns the
   phase graph. Sessions are scoped to the phase graph.
2. **MCP catalog is global, persona allowlist is the gate.** Mirrors the
   existing path-scope allowlist pattern, so the mental model is "personas
   compose capabilities from a shared catalog."
3. **`max_redispatches=1` default.** Two hard failures in a row almost
   always means the task spec is wrong; further retries waste tokens.
   Configurable per-task and globally.
4. **Handoff is generated by the same LLM backend as the planner**, not a
   separate cheap model. Keeps the dependency surface flat; cost is small
   (~1 call per redispatch).
5. **Cursor `agent_id` is not persisted to disk between dispatches** even
   for the same task. Redispatch always creates a fresh agent. Simpler;
   matches the "fresh agent_id with curated handoff" decision.
6. **kimi backend gets `NoopSession` + warning, not MCP feature parity.**
   Cursor is primary; kimi can catch up later if needed.

## Implementation order

Each step ships independently and is mergeable on its own.

1. `contracts/mcp.py` + `config.py` MCP catalog parsing + per-persona
   allowlist. No behavior change yet (allowlist is parsed but unused).
2. `sessions.py` with `AgentSession` protocol, `CursorSession`,
   `NoopSession`. `cursor_backend.py` gains `create_session()`; existing
   `run()` keeps working unchanged.
3. `runner.py` switches from per-phase `backend.run()` to per-task
   `session = backend.create_session(); session.send(...) × N;
   session.dispose()`. MCPs now flow through end-to-end.
4. `redispatch.py` + `dispatcher.py` wrapping. New `dispatch_with_retry()`
   becomes the default entrypoint; legacy `dispatch()` stays exported for
   tests.
5. Documentation: `docs/MCP.md` (catalog, allowlist, env vars, smoke
   test), `docs/SESSIONS.md` (lifecycle, what persists across phases),
   `docs/REDISPATCH.md` (triggers, handoff shape, budget tuning).

## Risk register

| Risk | Mitigation |
|---|---|
| Cursor session token usage grows unbounded across phases | Cursor manages context window server-side; if hit, fall back gracefully — verified by integration test. Document the ceiling once observed in practice. |
| Playwright/Chrome DevTools MCPs spawn browsers that leak on session crash | `CursorSession.dispose()` is called in a `try/finally` in `runner.py`; document that MCP processes are torn down by Cursor when the agent disposes. |
| Serena's LSP indexing is slow on first run for large repos | Document a pre-warm step in `docs/MCP.md`; consider caching the Serena index across worktrees in a follow-up. |
| Redispatch with handoff makes the planner "stuck" on the wrong frame | Cap at 1 by default; surface attempt count in PR description so humans can see when an issue was retried. |
| MCP config drift between repos | Catalog lives in `.agent-fleet.yaml` per repo; that's the source of truth. No global default. |

## Acceptance criteria

- A persona with `mcp_servers: [serena, context7]` can call those tools
  from any phase (verified by integration test that asserts tool-use
  events).
- Within one task, all phases share one `agent_id` (verified by log
  inspection or session callback).
- A task that experiences Cursor `status=expired` is automatically
  redispatched once with a fresh agent and a structured handoff;
  redispatch is visible in the task event stream.
- Existing tasks with no `mcp_servers` set behave exactly as v0.4.2
  (regression suite passes unchanged).
- All four MCPs (Playwright, Chrome DevTools, Context7, Serena) are
  documented with working `.agent-fleet.yaml` examples and a manual smoke
  test procedure.
