# MCP Support, Persistent Sessions, and Outer Redispatch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land v0.5.0 of agent_fleet: per-call MCP server support (Playwright, Chrome DevTools, Context7, Serena), one durable `Agent.create()` session per task spanning all phases, and a dispatcher-level redispatch loop on hard failure with curated handoff.

**Architecture:** New `AgentSession` protocol + `CursorSession` impl wraps the Cursor SDK's stateful agent handle. `cursor_backend.py` gains a `create_session()` factory that forwards `mcp_servers` into `AgentOptions`. `runner.py` opens one session per task and `send()`s through every phase. `redispatch.py` wraps `dispatch()` with a hard-failure retry that creates a fresh session and prepends a structured handoff.

**Tech Stack:** Python 3.11+, Cursor Python SDK (cursor-sdk), pytest, ruff, pyyaml.

**Reference spec:** `docs/superpowers/specs/2026-05-23-mcp-sessions-redispatch-design.md`.

**Dogfood tagging.** Each task is tagged `[FLEET]` (safely dispatchable to agent_fleet itself in a worktree) or `[MANUAL]` (carries bootstrapping risk — execute by hand). When dispatching `[FLEET]` tasks, scope the persona allowlist to the listed file paths.

---

## Task 1: McpServerSpec contract types [FLEET]

**Files:**
- Create: `agent_fleet/contracts/mcp.py`
- Modify: `agent_fleet/contracts/__init__.py`
- Test: `tests/test_mcp_contracts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_contracts.py
"""Tests for MCP server config dataclasses."""

from __future__ import annotations

import pytest

from agent_fleet.contracts.mcp import (
    HttpMcpServerSpec,
    McpServerSpec,
    StdioMcpServerSpec,
    parse_mcp_server_spec,
)


def test_stdio_spec_minimum_fields() -> None:
    spec = StdioMcpServerSpec(command="npx", args=("-y", "@playwright/mcp"))
    assert spec.command == "npx"
    assert spec.args == ("-y", "@playwright/mcp")
    assert spec.env == {}


def test_http_spec_with_headers() -> None:
    spec = HttpMcpServerSpec(
        url="https://mcp.context7.com/mcp",
        headers={"Authorization": "Bearer x"},
    )
    assert spec.url == "https://mcp.context7.com/mcp"
    assert spec.headers["Authorization"] == "Bearer x"


def test_parse_stdio_from_dict() -> None:
    raw = {"type": "stdio", "command": "uvx", "args": ["serena-mcp-server"]}
    spec = parse_mcp_server_spec("serena", raw)
    assert isinstance(spec, StdioMcpServerSpec)
    assert spec.command == "uvx"


def test_parse_http_from_dict() -> None:
    raw = {"type": "http", "url": "https://example.com/mcp"}
    spec = parse_mcp_server_spec("docs", raw)
    assert isinstance(spec, HttpMcpServerSpec)
    assert spec.url == "https://example.com/mcp"


def test_parse_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown MCP type"):
        parse_mcp_server_spec("bad", {"type": "websocket", "url": "x"})


def test_parse_requires_url_for_http() -> None:
    with pytest.raises(ValueError, match="url is required"):
        parse_mcp_server_spec("bad", {"type": "http"})


def test_parse_requires_command_for_stdio() -> None:
    with pytest.raises(ValueError, match="command is required"):
        parse_mcp_server_spec("bad", {"type": "stdio"})


def test_mcp_server_spec_is_union() -> None:
    # McpServerSpec is the union type used by callers.
    stdio: McpServerSpec = StdioMcpServerSpec(command="x")
    http: McpServerSpec = HttpMcpServerSpec(url="y")
    assert stdio.command == "x"
    assert http.url == "y"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_mcp_contracts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_fleet.contracts.mcp'`.

- [ ] **Step 3: Create the contracts file**

```python
# agent_fleet/contracts/mcp.py
"""Dataclasses describing MCP server configurations forwarded to the Cursor SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Union


@dataclass(frozen=True)
class StdioMcpServerSpec:
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True)
class HttpMcpServerSpec:
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    auth_client_id: str | None = None
    auth_client_secret: str | None = None
    auth_scopes: tuple[str, ...] = ()


McpServerSpec = Union[StdioMcpServerSpec, HttpMcpServerSpec]


def parse_mcp_server_spec(name: str, raw: Mapping[str, Any]) -> McpServerSpec:
    kind = str(raw.get("type") or "stdio").lower()
    if kind == "stdio":
        command = raw.get("command")
        if not command:
            raise ValueError(f"MCP {name!r}: command is required for stdio")
        return StdioMcpServerSpec(
            command=str(command),
            args=tuple(str(a) for a in raw.get("args") or ()),
            env=dict(raw.get("env") or {}),
            cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        )
    if kind in {"http", "sse"}:
        url = raw.get("url")
        if not url:
            raise ValueError(f"MCP {name!r}: url is required for {kind}")
        auth = raw.get("auth") or {}
        return HttpMcpServerSpec(
            url=str(url),
            headers=dict(raw.get("headers") or {}),
            auth_client_id=auth.get("client_id") or auth.get("CLIENT_ID"),
            auth_client_secret=auth.get("client_secret") or auth.get("CLIENT_SECRET"),
            auth_scopes=tuple(str(s) for s in (auth.get("scopes") or ())),
        )
    raise ValueError(f"MCP {name!r}: unknown MCP type {kind!r}")
```

- [ ] **Step 4: Re-export from package**

Modify `agent_fleet/contracts/__init__.py` — add at the end:

```python
from agent_fleet.contracts.mcp import (
    HttpMcpServerSpec,
    McpServerSpec,
    StdioMcpServerSpec,
    parse_mcp_server_spec,
)

__all__ = [*globals().get("__all__", []),  # type: ignore[name-defined]
           "HttpMcpServerSpec", "McpServerSpec", "StdioMcpServerSpec", "parse_mcp_server_spec"]
```

If `__init__.py` does not already define `__all__`, just append the explicit imports without the conditional.

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_mcp_contracts.py -v`
Expected: PASS (8 tests).

- [ ] **Step 6: Run the full test suite and linter**

Run: `pytest tests/ -q && ruff check agent_fleet/ tests/`
Expected: existing tests still pass, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add agent_fleet/contracts/mcp.py agent_fleet/contracts/__init__.py tests/test_mcp_contracts.py
git commit -m "feat(contracts): add MCP server spec dataclasses

Adds StdioMcpServerSpec, HttpMcpServerSpec, and parse_mcp_server_spec
helper. Foundation for per-call MCP support in cursor_backend."
```

---

## Task 2: MCP catalog and persona allowlist in config [FLEET]

**Files:**
- Modify: `agent_fleet/config.py`
- Test: `tests/test_config_mcp.py`

Persona allowlist references the catalog by name. Catalog values support env-var expansion via `${VAR}` syntax in `headers` and `env` strings.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_mcp.py
"""Tests for MCP catalog + per-persona allowlist parsing."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_fleet.config import load_fleet_config
from agent_fleet.contracts.mcp import HttpMcpServerSpec, StdioMcpServerSpec


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text(textwrap.dedent(body), encoding="utf-8")
    return cfg


def test_catalog_parses_stdio_and_http(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        mcp_servers:
          playwright:
            type: stdio
            command: npx
            args: ["-y", "@playwright/mcp@latest"]
          context7:
            type: http
            url: https://mcp.context7.com/mcp
            headers:
              Authorization: Bearer fixed-token
    """)
    fc = load_fleet_config(cfg)
    assert isinstance(fc.mcp_servers["playwright"], StdioMcpServerSpec)
    assert fc.mcp_servers["playwright"].command == "npx"
    assert isinstance(fc.mcp_servers["context7"], HttpMcpServerSpec)
    assert fc.mcp_servers["context7"].headers["Authorization"] == "Bearer fixed-token"


def test_env_var_expansion_in_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CONTEXT7_KEY", "super-secret")
    cfg = _write(tmp_path, """
        mcp_servers:
          context7:
            type: http
            url: https://mcp.context7.com/mcp
            headers:
              Authorization: Bearer ${CONTEXT7_KEY}
    """)
    fc = load_fleet_config(cfg)
    assert fc.mcp_servers["context7"].headers["Authorization"] == "Bearer super-secret"


def test_missing_env_var_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cfg = _write(tmp_path, """
        mcp_servers:
          context7:
            type: http
            url: https://mcp.context7.com/mcp
            headers:
              Authorization: Bearer ${MISSING_KEY}
    """)
    with pytest.raises(ValueError, match="MISSING_KEY"):
        load_fleet_config(cfg)


def test_persona_allowlist_resolves_against_catalog(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        mcp_servers:
          playwright:
            type: stdio
            command: npx
            args: ["-y", "@playwright/mcp"]
        personas:
          coder:
            prompt: coder.md
            mcp_servers: [playwright]
    """)
    fc = load_fleet_config(cfg)
    assert fc.personas["coder"].mcp_servers == ["playwright"]


def test_persona_allowlist_unknown_mcp_raises(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        mcp_servers:
          playwright:
            type: stdio
            command: npx
        personas:
          coder:
            prompt: coder.md
            mcp_servers: [does_not_exist]
    """)
    with pytest.raises(ValueError, match="does_not_exist"):
        load_fleet_config(cfg)


def test_no_mcp_section_yields_empty_catalog(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
        personas:
          coder:
            prompt: coder.md
    """)
    fc = load_fleet_config(cfg)
    assert fc.mcp_servers == {}
    assert fc.personas["coder"].mcp_servers == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_config_mcp.py -v`
Expected: FAIL — `FleetConfig` has no `mcp_servers` attribute and `PersonaSpec` has no `mcp_servers` field.

- [ ] **Step 3: Extend FleetConfig and PersonaSpec**

Modify `agent_fleet/config.py`:

Add at the top, after the existing imports:

```python
import os
import re

from agent_fleet.contracts.mcp import McpServerSpec, parse_mcp_server_spec
```

Add `mcp_servers` to `PersonaSpec`:

```python
@dataclass
class PersonaSpec:
    prompt: str
    model: str | None = None
    mode: str | None = None
    skill: str | None = None
    allowed_paths: list[str] = field(default_factory=list)
    extra_instructions: str = ""
    mcp_servers: list[str] = field(default_factory=list)  # NEW
```

Add `mcp_servers` to `FleetConfig`:

```python
@dataclass
class FleetConfig:
    # ... existing fields ...
    mcp_servers: dict[str, McpServerSpec] = field(default_factory=dict)  # NEW
```

Add helper functions above `_parse_persona_specs`:

```python
_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${VAR} occurrences in strings inside dicts/lists."""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in os.environ:
                raise ValueError(f"environment variable {var!r} required but not set")
            return os.environ[var]
        return _ENV_VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _parse_mcp_catalog(raw: dict[str, Any]) -> dict[str, McpServerSpec]:
    catalog: dict[str, McpServerSpec] = {}
    for name, entry in (raw or {}).items():
        if not isinstance(entry, dict):
            continue
        expanded = _expand_env(entry)
        catalog[name] = parse_mcp_server_spec(name, expanded)
    return catalog
```

Update `_parse_persona_specs` to include `mcp_servers`:

```python
def _parse_persona_specs(
    raw: dict[str, Any], catalog: dict[str, McpServerSpec]
) -> dict[str, PersonaSpec]:
    specs: dict[str, PersonaSpec] = {}
    for name, entry in (raw or {}).items():
        if isinstance(entry, str):
            specs[name] = PersonaSpec(prompt=entry)
            continue
        if not isinstance(entry, dict):
            continue
        mcp_names = list(entry.get("mcp_servers") or [])
        for mcp_name in mcp_names:
            if mcp_name not in catalog:
                raise ValueError(
                    f"persona {name!r} references unknown MCP server {mcp_name!r}; "
                    f"known: {sorted(catalog)}"
                )
        specs[name] = PersonaSpec(
            prompt=str(entry.get("prompt") or f"{name}.md"),
            model=entry.get("model"),
            mode=entry.get("mode"),
            skill=entry.get("skill"),
            allowed_paths=list(entry.get("allowed_paths") or []),
            extra_instructions=str(entry.get("extra_instructions") or ""),
            mcp_servers=mcp_names,
        )
    return specs
```

In `load_fleet_config`, after `data` is loaded and before persona parsing:

```python
    mcp_catalog = _parse_mcp_catalog(data.get("mcp_servers") or {})
```

Pass it into `_parse_persona_specs` and into the returned `FleetConfig`:

```python
        personas=_parse_persona_specs(data.get("personas") or {}, mcp_catalog),
        # ...
        mcp_servers=mcp_catalog,
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_config_mcp.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Run the full test suite and linter**

Run: `pytest tests/ -q && ruff check agent_fleet/ tests/`
Expected: existing tests still pass, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add agent_fleet/config.py tests/test_config_mcp.py
git commit -m "feat(config): MCP catalog + per-persona allowlist

Top-level mcp_servers: defines named MCP configs (stdio/http) with
env-var expansion. Personas opt in via mcp_servers: [name, ...] which
is validated against the catalog at load time."
```

---

## Task 3: AgentSession protocol and NoopSession [FLEET]

**Files:**
- Create: `agent_fleet/sessions.py`
- Test: `tests/test_sessions.py`

This task only adds the *protocol* and the no-op fallback. The Cursor implementation lands in Task 4 (manual).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sessions.py
"""Tests for AgentSession protocol and NoopSession."""

from __future__ import annotations

from pathlib import Path

from agent_fleet.contracts.mcp import StdioMcpServerSpec
from agent_fleet.cursor_backend import CursorLLMResult
from agent_fleet.sessions import AgentSession, NoopSession


def test_noop_session_satisfies_protocol() -> None:
    sess = NoopSession()
    assert isinstance(sess, AgentSession)
    assert sess.agent_id is None


def test_noop_session_send_returns_static_error() -> None:
    sess = NoopSession()
    result = sess.send("hi", max_tokens=100, timeout_s=10)
    assert isinstance(result, CursorLLMResult)
    assert result.exit_code == 1
    assert "NoopSession" in result.stderr


def test_noop_session_dispose_is_idempotent() -> None:
    sess = NoopSession()
    sess.dispose()
    sess.dispose()  # second call should not raise


def test_noop_session_accepts_mcp_servers_silently(
    capsys,  # noqa: ANN001
) -> None:
    sess = NoopSession(
        mcp_servers={"playwright": StdioMcpServerSpec(command="npx")},
        persona_name="coder",
    )
    sess.send("x", max_tokens=1, timeout_s=1)
    captured = capsys.readouterr()
    assert "NoopSession" in (captured.out + captured.err)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_sessions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_fleet.sessions'`.

- [ ] **Step 3: Create the sessions module**

```python
# agent_fleet/sessions.py
"""Per-task agent session abstraction wrapping the Cursor SDK's durable agent."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from agent_fleet.contracts.mcp import McpServerSpec
from agent_fleet.cursor_backend import CursorLLMResult


@runtime_checkable
class AgentSession(Protocol):
    """A long-lived agent handle scoped to a single task. Multiple phases
    issue successive `send()` calls into the same conversation."""

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


class NoopSession:
    """Fallback session for backends that do not support persistent agents.
    Records that it was used and returns a clear error on send()."""

    agent_id: str | None = None

    def __init__(
        self,
        *,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,
        persona_name: str | None = None,
    ) -> None:
        self._mcp_servers = dict(mcp_servers or {})
        self._persona = persona_name
        self._disposed = False
        if self._mcp_servers:
            print(
                f"NoopSession: persona={persona_name!r} configured "
                f"{len(self._mcp_servers)} MCP server(s) but this backend "
                f"does not support MCPs; they will be ignored.",
                file=sys.stderr,
            )

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
    ) -> CursorLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools
        return CursorLLMResult(
            stdout="",
            stderr="NoopSession: send() called on a backend without session support",
            exit_code=1,
            duration_s=0.0,
        )

    def dispose(self) -> None:
        self._disposed = True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_sessions.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full test suite and linter**

Run: `pytest tests/ -q && ruff check agent_fleet/ tests/`
Expected: existing tests still pass, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add agent_fleet/sessions.py tests/test_sessions.py
git commit -m "feat(sessions): add AgentSession protocol and NoopSession

Protocol-level abstraction for per-task agent handles. NoopSession is
the fallback for backends that lack persistent agent support; warns
when MCP servers are configured but unusable."
```

---

## Task 4: CursorSession implementation + factory on CursorBackend [MANUAL]

**Why manual:** This modifies `cursor_backend.py` — the module that the running fleet imports to talk to Cursor. Even though the agent edits a worktree, the test step requires `pip install -e .` semantics; a subtle bug here can break subsequent dispatches. Hand-execute and verify.

**Files:**
- Modify: `agent_fleet/cursor_backend.py`
- Test: `tests/test_cursor_session.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cursor_session.py
"""Tests for CursorSession lifecycle and MCP forwarding (fake SDK)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_fleet.contracts.mcp import HttpMcpServerSpec, StdioMcpServerSpec
from agent_fleet.cursor_backend import CursorBackend, CursorLLMResult


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch):
    """Patch the cursor_sdk import inside CursorBackend to a fake module."""
    fake = MagicMock()
    fake.Agent.create.return_value = MagicMock(
        agent_id="agent-xyz",
        send=MagicMock(return_value=MagicMock(
            result="ok output", agent_id="agent-xyz", status="finished"
        )),
        dispose=MagicMock(),
    )
    fake.StdioMcpServerConfig = lambda **kw: ("stdio", kw)
    fake.HttpMcpServerConfig = lambda **kw: ("http", kw)
    fake.LocalAgentOptions = lambda **kw: ("local", kw)
    monkeypatch.setitem(__import__("sys").modules, "cursor_sdk", fake)
    return fake


def test_create_session_forwards_mcp_servers(fake_sdk, tmp_path: Path) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(
        persona_name="coder",
        cwd=tmp_path,
        mcp_servers={
            "playwright": StdioMcpServerSpec(command="npx", args=("-y", "x")),
            "context7": HttpMcpServerSpec(url="https://x", headers={"A": "B"}),
        },
    )
    assert sess.agent_id == "agent-xyz"
    args, kwargs = fake_sdk.Agent.create.call_args
    assert "mcp_servers" in kwargs
    assert set(kwargs["mcp_servers"]) == {"playwright", "context7"}


def test_session_send_returns_cursor_llm_result(fake_sdk, tmp_path: Path) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("do work", max_tokens=1000, timeout_s=60)
    assert isinstance(result, CursorLLMResult)
    assert result.exit_code == 0
    assert result.stdout == "ok output"
    assert result.agent_id == "agent-xyz"


def test_session_send_maps_error_status_to_nonzero_exit(
    fake_sdk, tmp_path: Path
) -> None:
    fake_sdk.Agent.create.return_value.send.return_value = MagicMock(
        result="partial", agent_id="agent-xyz", status="expired"
    )
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("hi", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert "expired" in result.stderr


def test_session_dispose_calls_sdk_dispose(fake_sdk, tmp_path: Path) -> None:
    backend = CursorBackend(api_key="x")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    sess.dispose()
    sess.dispose()  # idempotent
    fake_sdk.Agent.create.return_value.dispose.assert_called_once()


def test_create_session_returns_error_session_without_api_key(tmp_path: Path) -> None:
    backend = CursorBackend(api_key="")
    sess = backend.create_session(persona_name="coder", cwd=tmp_path)
    result = sess.send("x", max_tokens=1, timeout_s=1)
    assert result.exit_code == 1
    assert "CURSOR_API_KEY" in result.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_cursor_session.py -v`
Expected: FAIL — `CursorBackend` has no `create_session` method.

- [ ] **Step 3: Add CursorSession class and create_session factory**

Open `agent_fleet/cursor_backend.py`. Above the existing `class CursorBackend:` definition, add:

```python
from agent_fleet.contracts.mcp import (
    HttpMcpServerSpec,
    McpServerSpec,
    StdioMcpServerSpec,
)


def _sdk_mcp_config(spec: McpServerSpec, sdk):  # noqa: ANN001
    if isinstance(spec, StdioMcpServerSpec):
        return sdk.StdioMcpServerConfig(
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env),
            cwd=spec.cwd,
        )
    return sdk.HttpMcpServerConfig(
        url=spec.url,
        headers=dict(spec.headers),
        # auth is optional; only pass if any field is set
        auth=(
            sdk.McpAuth(
                client_id=spec.auth_client_id,
                client_secret=spec.auth_client_secret,
                scopes=list(spec.auth_scopes),
            )
            if spec.auth_client_id
            else None
        ),
    )


class CursorSession:
    """Durable Cursor agent handle scoped to one task."""

    def __init__(
        self,
        agent,  # noqa: ANN001 - SDK type
        *,
        default_timeout_s: int,
    ) -> None:
        self._agent = agent
        self._default_timeout_s = default_timeout_s
        self._disposed = False
        self.agent_id: str | None = getattr(agent, "agent_id", None)

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
    ) -> CursorLLMResult:
        del max_tokens
        scope_note = ""
        if allowed_tools:
            scoped = [t.removeprefix("path:") for t in allowed_tools if t.startswith("path:")]
            if scoped:
                scope_note = (
                    "\n\nHard scope constraint: only modify files under: "
                    + ", ".join(scoped)
                )
        body = f"{prompt}{scope_note}" if scope_note else prompt
        t0 = time.monotonic()
        try:
            result = self._agent.send(body)
            text = getattr(result, "result", None) or str(result)
            status = getattr(result, "status", "finished")
            agent_id = getattr(result, "agent_id", self.agent_id)
            duration_s = time.monotonic() - t0
            if status in {"error", "cancelled", "expired"}:
                return CursorLLMResult(
                    stdout=text or "",
                    stderr=f"Cursor send status: {status}",
                    exit_code=1,
                    duration_s=duration_s,
                    agent_id=agent_id,
                )
            return CursorLLMResult(
                stdout=text or "",
                stderr="",
                exit_code=0,
                duration_s=duration_s,
                agent_id=agent_id,
            )
        except Exception as exc:  # noqa: BLE001
            return CursorLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
                agent_id=self.agent_id,
            )

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self._agent.dispose()
        except Exception:  # noqa: BLE001
            pass


class _ErrorSession:
    """Stub session that always fails — used when API key is missing."""

    agent_id: str | None = None

    def __init__(self, message: str) -> None:
        self._message = message

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
    ) -> CursorLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools
        return CursorLLMResult(
            stdout="", stderr=self._message, exit_code=1, duration_s=0.0
        )

    def dispose(self) -> None:
        return
```

Then add to the existing `CursorBackend` class:

```python
    def create_session(
        self,
        *,
        persona_name: str,
        cwd: Path,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,
        model: str | None = None,
        mode: AgentMode | None = None,
    ):
        if not self.api_key:
            return _ErrorSession("CURSOR_API_KEY is not set")
        try:
            import cursor_sdk as sdk
        except ImportError as exc:
            return _ErrorSession(f"cursor-sdk not installed: {exc}")
        selected_model = model or self.default_model
        selected_mode = coerce_agent_mode(mode, default=self.default_mode)
        mcp_dict = {
            name: _sdk_mcp_config(spec, sdk)
            for name, spec in (mcp_servers or {}).items()
        }
        try:
            agent = sdk.Agent.create(
                model=selected_model,
                api_key=self.api_key,
                local=sdk.LocalAgentOptions(cwd=str(cwd)),
                mcp_servers=mcp_dict or None,
                mode=selected_mode,
                name=f"fleet:{persona_name}",
            )
        except Exception as exc:  # noqa: BLE001
            return _ErrorSession(f"Agent.create failed: {exc}")
        return CursorSession(agent, default_timeout_s=900)
```

You'll also need to add `from typing import Mapping` to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cursor_session.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full test suite and linter**

Run: `pytest tests/ -q && ruff check agent_fleet/ tests/`
Expected: existing tests still pass, no lint errors.

- [ ] **Step 6: Manual smoke test (only if CURSOR_API_KEY is set)**

Run from the worktree root:

```bash
python - <<'PY'
import os
from pathlib import Path
from agent_fleet.cursor_backend import CursorBackend
from agent_fleet.contracts.mcp import StdioMcpServerSpec

backend = CursorBackend()
sess = backend.create_session(
    persona_name="smoke",
    cwd=Path.cwd(),
    mcp_servers={
        "playwright": StdioMcpServerSpec(
            command="npx", args=("-y", "@playwright/mcp@latest")
        ),
    },
)
print("agent_id =", sess.agent_id)
result = sess.send("Say hello.", max_tokens=200, timeout_s=60)
print("exit", result.exit_code, "stdout:", result.stdout[:200])
sess.dispose()
PY
```

Expected: `agent_id` is non-None, exit_code=0, stdout contains a greeting. If this fails because Playwright MCP isn't available, retry with `mcp_servers={}` to isolate.

- [ ] **Step 7: Commit**

```bash
git add agent_fleet/cursor_backend.py tests/test_cursor_session.py
git commit -m "feat(cursor): CursorSession with mcp_servers wiring

CursorBackend.create_session() returns a durable session that wraps
Cursor SDK Agent.create() with mcp_servers forwarded from the
catalog. Maps SDK status=error/cancelled/expired to exit_code=1."
```

---

## Task 5: Thread sessions through runner phases [MANUAL]

**Why manual:** Cross-file structural change touching `runner.py`, `phases.py`, `planner.py`, `researcher.py`, `synthesizer.py`, `implementer.py`, `reviewer.py`, `tech_lead.py`. Cannot be safely scope-allowlisted to a single persona; high risk of partial refactor.

**Files:**
- Modify: `agent_fleet/runner.py`
- Modify: `agent_fleet/phases.py:63`, `phases.py:446`
- Modify: `agent_fleet/planner.py:178`
- Modify: `agent_fleet/researcher.py:107,191`
- Modify: `agent_fleet/synthesizer.py:241`
- Modify: `agent_fleet/implementer.py:71`
- Modify: `agent_fleet/reviewer.py:134`
- Modify: `agent_fleet/tech_lead.py:165`
- Modify: `agent_fleet/hooks.py` (add `LLMSession` protocol)
- Test: `tests/test_runner_sessions.py`

This task introduces a `session: AgentSession | None` parameter alongside the existing `backend: LLMBackend` argument in every phase function. The default is None for backward compatibility; when present, the phase calls `session.send(prompt, ...)` instead of `backend.run(prompt, ...)`. `runner.run()` is the single creation/disposal site.

- [ ] **Step 1: Write the failing test (runner end-to-end with session)**

```python
# tests/test_runner_sessions.py
"""Verify runner opens one session per task and routes all phase prompts through it."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_fleet.cursor_backend import CursorLLMResult


class FakeBackend:
    """Backend exposing create_session — runner should detect it and use sessions."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.agent_id = "agent-test"
        self.session.send.return_value = CursorLLMResult(
            stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id="agent-test"
        )
        self.create_session_calls: list[dict] = []

    def create_session(self, **kwargs):  # noqa: ANN003, ANN201
        self.create_session_calls.append(kwargs)
        return self.session


def test_runner_simple_pipeline_uses_one_session(
    tmp_path: Path, fleet_config_with_session_backend  # noqa: ANN001
) -> None:
    """For the simple pipeline (single execute phase), runner must:
    1) call backend.create_session() exactly once
    2) call session.send() exactly once
    3) call session.dispose() in the finally block
    4) NOT call backend.run() at all
    """
    backend = FakeBackend()
    runner = fleet_config_with_session_backend(backend)
    runner.run(task_id=1, pipeline="simple")
    assert len(backend.create_session_calls) == 1
    assert backend.session.send.call_count == 1
    assert backend.session.dispose.call_count == 1


def test_runner_falls_back_to_backend_run_when_no_create_session(tmp_path: Path) -> None:
    """Backends without create_session() must still work (legacy path)."""
    legacy = MagicMock(spec=["run"])  # no create_session attribute
    legacy.run.return_value = CursorLLMResult(
        stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )
    # ... construct runner with legacy backend ...
    # ... assert legacy.run was called and no session machinery executed ...
```

The `fleet_config_with_session_backend` fixture should be added to `tests/conftest.py` following the same `load_fleet_config(ROOT / "fleet.example.yaml")` + `YamlPersonaResolver` pattern used in `tests/test_fleet.py:30-32`. Wire a single-task `FleetTask` into a `TaskRunner` constructed with the FakeBackend.

- [ ] **Step 2: Add LLMSession to hooks.py**

```python
# agent_fleet/hooks.py — add after LLMBackend
@runtime_checkable
class LLMSession(Protocol):
    agent_id: str | None

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
    ) -> LLMResult: ...

    def dispose(self) -> None: ...
```

- [ ] **Step 3: Add `session` kwarg to every phase function**

For each of: `phases._run_implement` (line ~63), `phases._run_review` (~446), `planner.run_planner`, `researcher.run_researcher` (both call sites), `synthesizer.run_synthesizer`, `implementer.run_implementer`, `reviewer.run_reviewer`, `tech_lead.run_tech_lead` — add a `session: LLMSession | None = None` keyword argument and conditionally route:

```python
if session is not None:
    result = session.send(prompt, max_tokens=max_tokens, timeout_s=timeout_s, allowed_tools=allowed_tools)
else:
    result = backend.run(prompt, max_tokens=max_tokens, timeout_s=timeout_s, allowed_tools=allowed_tools, cwd=cwd, model=model, mode=mode)
```

Keep `cwd`, `model`, `mode` flowing into backend.run() unchanged for the legacy path.

- [ ] **Step 4: Open and dispose session in runner.run()**

In `agent_fleet/runner.py`, at the top of `TaskRunner.run()` (line ~148):

```python
session = None
if hasattr(self._backend, "create_session"):
    persona = self._persona_resolver.load(task.persona)
    mcp_specs = {
        name: self._fleet_config.mcp_servers[name]
        for name in persona.mcp_servers
        if name in self._fleet_config.mcp_servers
    }
    session = self._backend.create_session(
        persona_name=task.persona,
        cwd=worktree,
        mcp_servers=mcp_specs,
        model=task.model,
        mode=task.mode,
    )
try:
    # ... existing phase loop, threading `session=session` into each call ...
finally:
    if session is not None:
        session.dispose()
```

Note: `persona.mcp_servers` requires PersonaResolver/Persona dataclasses to expose the field. Update `personas.py` to copy `mcp_servers` from `PersonaSpec` onto the loaded `Persona`.

- [ ] **Step 5: Run runner tests**

Run: `pytest tests/test_runner_sessions.py tests/test_fleet.py -v`
Expected: PASS. Regressions in `test_fleet.py` mean the legacy `backend.run` path was broken.

- [ ] **Step 6: Manual smoke**

```bash
fleet dispatch --pipeline simple --persona coder \
    --workspace /tmp/scratch-repo \
    "echo hello to README.md"
```

Confirm log emits one `session.send` per phase, one `agent_id` shared across them.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(runner): one AgentSession per task across phases

Runner opens session via backend.create_session() at task start,
threads it into every phase call, disposes in finally. MCPs and
agent_id now persist across plan → research → synthesize → implement
→ verify → review."
```

---

## Task 6: Redispatch module [FLEET]

**Files:**
- Create: `agent_fleet/redispatch.py`
- Create: `agent_fleet/contracts/handoff.py`
- Test: `tests/test_redispatch.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_redispatch.py
"""Tests for hard-failure detection, handoff extraction, and the retry loop."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent_fleet.contracts.handoff import HandoffNote
from agent_fleet.redispatch import (
    _extract_handoff,
    _is_hard_failure,
    dispatch_with_retry,
)


@dataclass
class FakeResult:
    status: str
    files_modified: tuple[str, ...] = ()
    stderr: str = ""
    exit_code: int = 0


@pytest.mark.parametrize(
    "status, exit_code, expected",
    [
        ("error", 1, True),
        ("cancelled", 1, True),
        ("expired", 1, True),
        ("timeout", 1, True),
        ("scope_violation", 1, True),
        ("pipeline_nonzero", 2, True),
        ("verify_failed", 0, False),
        ("review_rejected", 0, False),
        ("success", 0, False),
    ],
)
def test_is_hard_failure_table(
    status: str, exit_code: int, expected: bool
) -> None:
    r = FakeResult(status=status, exit_code=exit_code)
    assert _is_hard_failure(r) is expected


def test_extract_handoff_captures_failure_context() -> None:
    r = FakeResult(
        status="expired",
        files_modified=("src/a.py", "src/b.py"),
        stderr="Cursor send status: expired",
    )
    note = _extract_handoff(r, previous=None)
    assert isinstance(note, HandoffNote)
    assert "expired" in note.failure_mode
    assert "src/a.py" in note.files_touched
    assert note.attempt_number == 1


def test_extract_handoff_chains_attempts() -> None:
    first = _extract_handoff(
        FakeResult(status="error", stderr="x"), previous=None
    )
    second = _extract_handoff(
        FakeResult(status="error", stderr="y"), previous=first
    )
    assert second.attempt_number == 2


def test_dispatch_with_retry_succeeds_first_try() -> None:
    calls = []

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001
        calls.append(handoff)
        return FakeResult(status="success")

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=1)
    assert result.status == "success"
    assert calls == [None]


def test_dispatch_with_retry_redispatches_on_hard_failure() -> None:
    statuses = iter(["expired", "success"])
    handoffs_seen = []

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001
        handoffs_seen.append(handoff)
        return FakeResult(status=next(statuses))

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=1)
    assert result.status == "success"
    assert handoffs_seen[0] is None
    assert handoffs_seen[1] is not None
    assert handoffs_seen[1].attempt_number == 1


def test_dispatch_with_retry_does_not_redispatch_soft_failure() -> None:
    calls = 0

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return FakeResult(status="verify_failed")

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=3)
    assert result.status == "verify_failed"
    assert calls == 1  # only the initial attempt


def test_dispatch_with_retry_respects_budget() -> None:
    calls = 0

    def fake_dispatch(task, *, handoff=None):  # noqa: ANN001
        nonlocal calls
        calls += 1
        return FakeResult(status="expired")

    result = dispatch_with_retry({"id": 1}, dispatch=fake_dispatch, max_redispatches=2)
    assert result.status == "expired"
    assert calls == 3  # 1 initial + 2 redispatches
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_redispatch.py -v`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Create handoff contract**

```python
# agent_fleet/contracts/handoff.py
"""Structured summary fed into a redispatched task's planner."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HandoffNote:
    failure_mode: str
    files_touched: tuple[str, ...]
    stderr_tail: str
    summary: str
    attempt_number: int = 1
    previous: "HandoffNote | None" = None

    def render(self) -> str:
        prior = (
            f"\n\n(This is attempt #{self.attempt_number + 1}; prior attempts also failed.)"
            if self.previous
            else ""
        )
        files = (
            "Files modified before reset: " + ", ".join(self.files_touched)
            if self.files_touched
            else "No files were modified."
        )
        return (
            "PREVIOUS ATTEMPT CONTEXT — read carefully before planning.\n"
            f"Failure mode: {self.failure_mode}\n"
            f"{files}\n"
            f"Last stderr (truncated): {self.stderr_tail[-500:]}\n"
            f"Summary: {self.summary}"
            f"{prior}"
        )
```

- [ ] **Step 4: Create redispatch module**

```python
# agent_fleet/redispatch.py
"""Outer retry loop reacting to hard task failures with curated handoff."""

from __future__ import annotations

from typing import Any, Callable, Protocol

from agent_fleet.contracts.handoff import HandoffNote


_HARD_STATUSES = frozenset(
    {"error", "cancelled", "expired", "timeout", "scope_violation", "pipeline_nonzero"}
)


class _ResultLike(Protocol):
    status: str
    files_modified: tuple[str, ...]
    stderr: str
    exit_code: int


def _is_hard_failure(result: Any) -> bool:
    status = getattr(result, "status", "")
    exit_code = getattr(result, "exit_code", 0)
    return status in _HARD_STATUSES or exit_code not in (0,)


def _extract_handoff(result: Any, *, previous: HandoffNote | None) -> HandoffNote:
    status = getattr(result, "status", "error")
    files = tuple(getattr(result, "files_modified", ()) or ())
    stderr = str(getattr(result, "stderr", ""))
    attempt = (previous.attempt_number + 1) if previous else 1
    summary = (
        f"Previous attempt ended with status={status!r}. "
        f"Modified {len(files)} file(s) before reset. "
        "Do not repeat the same approach blindly; analyze the stderr above "
        "and plan around the failure mode."
    )
    return HandoffNote(
        failure_mode=status,
        files_touched=files,
        stderr_tail=stderr,
        summary=summary,
        attempt_number=attempt,
        previous=previous,
    )


def dispatch_with_retry(
    task: Any,
    *,
    dispatch: Callable[..., Any],
    max_redispatches: int = 1,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    handoff: HandoffNote | None = None
    result = None
    for attempt in range(max_redispatches + 1):
        if on_event is not None:
            on_event(
                "redispatch.attempt",
                {"attempt": attempt, "has_handoff": handoff is not None},
            )
        result = dispatch(task, handoff=handoff)
        if not _is_hard_failure(result):
            return result
        if attempt == max_redispatches:
            break
        handoff = _extract_handoff(result, previous=handoff)
    return result
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_redispatch.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Run the full test suite and linter**

Run: `pytest tests/ -q && ruff check agent_fleet/ tests/`
Expected: existing tests still pass, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add agent_fleet/redispatch.py agent_fleet/contracts/handoff.py tests/test_redispatch.py
git commit -m "feat(redispatch): hard-failure retry with curated handoff

Outer loop that retries dispatch() on Cursor error/cancelled/expired/
timeout/scope_violation/non-zero exit. Soft failures (verify, review)
do not trigger. Each retry receives a HandoffNote summarizing the
prior attempt."
```

---

## Task 7: Wire redispatch into Dispatcher and CLI [FLEET]

**Files:**
- Modify: `agent_fleet/dispatcher.py`
- Modify: `agent_fleet/cli.py`
- Modify: `agent_fleet/config.py` (add `max_redispatches` config field)
- Test: `tests/test_dispatcher_redispatch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dispatcher_redispatch.py
"""End-to-end: Dispatcher.dispatch() should redispatch hard failures."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class FakeRunResult:
    status: str
    exit_code: int = 0
    files_modified: tuple[str, ...] = ()
    stderr: str = ""


def test_dispatch_retries_once_on_hard_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch Dispatcher._run_one to fail then succeed; assert two calls + handoff on second."""
    from agent_fleet.dispatcher import Dispatcher

    calls: list[object] = []
    statuses = iter([
        FakeRunResult(status="expired", exit_code=1, stderr="Cursor expired"),
        FakeRunResult(status="success", exit_code=0),
    ])

    def fake_run_one(self, task, *, handoff=None):  # noqa: ANN001
        calls.append(handoff)
        return next(statuses)

    monkeypatch.setattr(Dispatcher, "_run_one", fake_run_one)

    # Construct dispatcher with minimal config (uses fixture from test_fleet.py
    # — see fleet_config fixture there). max_redispatches=1 (the default).
    dispatcher = _make_dispatcher_for_test(max_redispatches=1)
    result = dispatcher.dispatch(_fake_task())
    assert result.status == "success"
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None  # handoff threaded through


def test_dispatch_does_not_retry_soft_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify_failed must NOT trigger a retry even with budget > 0."""
    from agent_fleet.dispatcher import Dispatcher

    call_count = 0

    def fake_run_one(self, task, *, handoff=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return FakeRunResult(status="verify_failed", exit_code=0)

    monkeypatch.setattr(Dispatcher, "_run_one", fake_run_one)
    dispatcher = _make_dispatcher_for_test(max_redispatches=3)
    dispatcher.dispatch(_fake_task())
    assert call_count == 1


def test_dispatch_respects_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """All attempts fail → returns after max_redispatches + 1 calls."""
    from agent_fleet.dispatcher import Dispatcher

    call_count = 0

    def fake_run_one(self, task, *, handoff=None):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return FakeRunResult(status="expired", exit_code=1)

    monkeypatch.setattr(Dispatcher, "_run_one", fake_run_one)
    dispatcher = _make_dispatcher_for_test(max_redispatches=2)
    result = dispatcher.dispatch(_fake_task())
    assert call_count == 3
    assert result.status == "expired"


# --- helpers ---


def _make_dispatcher_for_test(*, max_redispatches: int):  # noqa: ANN201
    """Construct a Dispatcher with the fleet_example.yaml fixture, overriding
    max_redispatches. Follow the pattern in tests/test_fleet.py:30 to load
    FleetConfig and the YamlPersonaResolver."""
    raise NotImplementedError(
        "Wire up using fleet_config fixture from test_fleet.py and override "
        "fc.max_redispatches before constructing Dispatcher"
    )


def _fake_task():  # noqa: ANN201
    """Minimal FleetTask suitable for dispatching. Use _normalize_tasks() helper
    from dispatcher.py to convert a dict spec to a FleetTask."""
    raise NotImplementedError(
        "Construct via agent_fleet.dispatcher._normalize_tasks([{...}])"
    )
```

These helpers raise `NotImplementedError` so the test file fails loudly at first run; the implementer must fill them in by following the patterns in `tests/test_fleet.py` (the `fleet_config` fixture at line 30) and `agent_fleet/dispatcher.py:46` (`_normalize_tasks`).

- [ ] **Step 2: Add `max_redispatches` to FleetConfig**

In `agent_fleet/config.py`:

```python
@dataclass
class FleetConfig:
    # ... existing fields ...
    max_redispatches: int = 1
```

Parse from YAML in `load_fleet_config()`:

```python
        max_redispatches=int(data.get("max_redispatches") or 1),
```

- [ ] **Step 3: Wrap `_run_one` in `dispatch_with_retry`**

In `agent_fleet/dispatcher.py`, in the `dispatch()` method (~line 429), replace the direct call to `self._run_one(task)` with:

```python
from agent_fleet.redispatch import dispatch_with_retry

def _run_with_handoff(t, *, handoff=None):  # noqa: ANN001
    return self._run_one(t, handoff=handoff)

result = dispatch_with_retry(
    task,
    dispatch=_run_with_handoff,
    max_redispatches=self._fleet_config.max_redispatches,
    on_event=lambda evt, payload: self._emit(evt, **payload),
)
```

Update `_run_one` to accept and forward a `handoff` kwarg through to the runner; in the runner, when `handoff` is set, prepend `handoff.render()` to the planner prompt (or, for `simple` pipelines, to the single execute prompt).

- [ ] **Step 4: Add CLI override**

In `agent_fleet/cli.py`, add `--max-redispatches` to the dispatch subparser:

```python
dispatch_parser.add_argument(
    "--max-redispatches",
    type=int,
    default=None,
    help="Override fleet config max_redispatches for this run.",
)
```

Thread it through `dispatch_tasks(..., max_redispatches=args.max_redispatches)`.

- [ ] **Step 5: Run the test suite**

Run: `pytest tests/ -q && ruff check agent_fleet/ tests/`
Expected: pass, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add agent_fleet/dispatcher.py agent_fleet/cli.py agent_fleet/config.py tests/test_dispatcher_redispatch.py
git commit -m "feat(dispatcher): wire redispatch loop around _run_one

Dispatcher.dispatch() now retries hard failures via dispatch_with_retry.
Budget configurable via fleet.yaml max_redispatches (default 1) and
overridable per-run via --max-redispatches."
```

---

## Task 8: Documentation [FLEET]

**Files:**
- Create: `docs/MCP.md`
- Create: `docs/SESSIONS.md`
- Create: `docs/REDISPATCH.md`
- Modify: `fleet.example.yaml` — add mcp_servers section and per-persona mcp_servers allowlist
- Modify: `README.md` — add "v0.5.0 highlights" link section

Each doc covers: what it does, configuration, examples, smoke-test procedure, troubleshooting.

- [ ] **Step 1: Create `docs/MCP.md`**

Sections to include:
1. **What** — explain that MCP servers extend agent capability per-persona.
2. **Catalog format** — show full `mcp_servers:` YAML block with stdio and http examples.
3. **Per-persona allowlist** — show `personas.coder.mcp_servers: [name, ...]`.
4. **Bundled recipes** — exact YAML for Playwright, Chrome DevTools, Context7, Serena.
5. **Env-var expansion** — `${VAR}` syntax and failure mode.
6. **Smoke test** — `python -c "..."` script that creates a session with one MCP and runs a one-line prompt.
7. **Troubleshooting** — common failures (PATH issues, auth, MCP server crashes).

- [ ] **Step 2: Create `docs/SESSIONS.md`**

Sections:
1. **What persists** — agent_id, MCP tool state, conversation history across phases.
2. **What doesn't** — agent_id across tasks, agent_id across redispatches.
3. **Lifecycle diagram** — runner opens → phases send → finally dispose.
4. **Failure modes** — Cursor expiry, dispose-on-crash, fallback to legacy `backend.run`.

- [ ] **Step 3: Create `docs/REDISPATCH.md`**

Sections:
1. **Triggers** — exact list of hard-failure conditions.
2. **What does NOT trigger** — soft failures, explanation why.
3. **Handoff shape** — render output example.
4. **Budget tuning** — when to raise from 1 (slow Cursor periods) and when to disable (CI runs).

- [ ] **Step 4: Update `fleet.example.yaml`**

Add a fully-fleshed `mcp_servers:` block with all four MCPs and update at least one persona (`coder`) to use them.

- [ ] **Step 5: Update README.md**

Add a "v0.5.0" section listing the four features with links to the three new docs and the spec.

- [ ] **Step 6: Commit**

```bash
git add docs/MCP.md docs/SESSIONS.md docs/REDISPATCH.md fleet.example.yaml README.md
git commit -m "docs: v0.5.0 — MCP catalog, sessions, redispatch

Documents the four v0.5.0 features end-to-end with smoke tests and
troubleshooting. Updates fleet.example.yaml to showcase Playwright,
Chrome DevTools, Context7, and Serena."
```

---

## Self-Review

After all tasks complete, run from repo root:

```bash
pytest tests/ -v
ruff check agent_fleet/ tests/
```

Then cut release:

```bash
# Update __init__.py version to 0.5.0
git tag v0.5.0
git push origin main --tags
```

silphco bumps the pin in `agents/pyproject.toml` from `@v0.4.2` to `@v0.5.0` and refreshes `.agent-fleet.yaml` with the new `mcp_servers:` block.

## Coverage map (spec → tasks)

| Spec section | Task |
|---|---|
| MCP wiring (Cursor SDK `mcp_servers`) | Tasks 1, 2, 4 |
| `AgentSession` protocol | Task 3 |
| `CursorSession` impl | Task 4 |
| `NoopSession` for kimi backend | Task 3 |
| Per-task agent across phases (runner) | Task 5 |
| Hard-failure redispatch | Tasks 6, 7 |
| Curated handoff | Task 6 |
| MCP catalog config | Task 2 |
| Persona allowlist | Task 2 |
| Env-var expansion | Task 2 |
| Configurable budget | Task 7 |
| Documentation + smoke tests | Task 8 |
