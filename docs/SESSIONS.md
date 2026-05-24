# Persistent Agent Sessions

In v0.5.0 agent_fleet switched from one-shot `Agent.prompt()` calls (one per phase) to a
durable `Agent.create()` + repeated `send()` pattern. Each task now owns one long-lived
agent handle — a **session** — that is opened before the first phase and disposed after the
last phase, regardless of outcome.

## What persists across phases

Within a single task run, all phases share:

| Thing | Shared? | Notes |
|-------|---------|-------|
| `agent_id` | yes | The same Cursor agent handles all turns (plan, research, implement, verify, review) |
| Conversation history | yes | Later phases can read earlier phases' output in the same context window |
| MCP tool state | yes | Playwright browser instance, Serena index, etc. persist across `send()` calls |

This means a researcher can hand off Context7 search results to the implementer without
re-serializing them as text, and the reviewer sees the full conversation that led to the
code change.

## What does NOT persist

| Thing | Shared? | Why |
|-------|---------|-----|
| `agent_id` across tasks | no | Each task calls `create_session()` independently |
| `agent_id` across redispatches | no | A hard failure triggers `session.dispose()`, then a fresh `create_session()` for the retry — by design, so the new agent starts with a clean slate guided only by the curated handoff note |
| MCP tool state across tasks | no | MCP processes are torn down when the session is disposed |

## Lifecycle

```
runner.run_task(task)
│
├── session = backend.create_session(persona, cwd, mcp_servers)
│       └── SDK: Agent.create(...) → agent_id = "A1"
│
├── [plan phase]     session.send(planner_prompt)     ← turn 1 on A1
├── [research phase] session.send(researcher_prompt)  ← turn 2 on A1 (MCPs usable)
├── [implement phase]session.send(implementer_prompt) ← turn 3 on A1 (MCPs usable)
├── [verify phase]   session.send(verify_prompt)      ← turn 4+ on A1
├── [review phase]   session.send(reviewer_prompt)    ← final turn on A1
│
└── finally: session.dispose()    ← called on success, failure, AND exception
```

The `finally` block guarantees `dispose()` is always called. MCP server processes started
by the Cursor SDK are torn down when the agent is disposed, even if a phase crashes.

### Legacy fallback

Backends that don't implement `create_session()` (e.g. a custom backend from before v0.5.0)
fall back to the old one-shot `backend.run()` path per phase. The dispatcher checks for the
method before using it:

```python
if hasattr(self._backend, "create_session"):
    session = self._backend.create_session(...)
    # use session.send() across phases
else:
    # legacy: call backend.run() per phase (no shared agent_id, no MCP persistence)
```

The kimi backend uses `NoopSession`, which accepts the `send()` call interface but executes
a one-shot CLI invocation each time (today's behavior). MCPs configured for a persona that
gets routed to kimi produce a warning on stderr and are otherwise ignored.

## Failure modes

### Cursor SDK expiry mid-task

If `session.send()` returns `exit_code=1` with `stderr="Cursor send status: expired"`, the
runner surfaces this as a hard failure. The `finally: session.dispose()` still runs, then
the dispatcher's redispatch loop fires (see [docs/REDISPATCH.md](REDISPATCH.md)).

The failed session's `agent_id` is captured into the `HandoffNote` along with any files
modified before the expiry.

### Missing CURSOR_API_KEY

`CursorBackend.create_session()` checks for the API key before calling the SDK. If the key
is absent it returns an `_ErrorSession` immediately (no SDK call, no network request).
Every subsequent `send()` on an `_ErrorSession` returns `exit_code=1` with the message
`"CURSOR_API_KEY is not set"`.

```bash
# Verify the key is exported before running agent-fleet:
echo $CURSOR_API_KEY   # should print a non-empty string
```

### Exception inside a phase

If a phase raises an unhandled exception, the runner's `try/finally` still calls
`session.dispose()` before re-raising, so MCP processes are cleaned up. The exception
propagates to the dispatcher which records it as `status="error"`.

## The AgentSession protocol

Custom backends can implement the protocol to gain first-class session support:

```python
from typing import Protocol, runtime_checkable
from agent_fleet.cursor_backend import CursorLLMResult

@runtime_checkable
class AgentSession(Protocol):
    agent_id: str | None

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

`CursorSession` and `NoopSession` (in `agent_fleet/sessions.py`) are the two built-in
implementations. `CursorSession` wraps a real Cursor SDK agent handle; `NoopSession` is the
fallback for backends without persistent-agent support.
