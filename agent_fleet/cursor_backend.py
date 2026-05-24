"""Cursor SDK backend implementing the fleet LLMBackend protocol."""

from __future__ import annotations

import contextlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.agent_mode import AgentMode, coerce_agent_mode
from agent_fleet.contracts.mcp import McpServerSpec, StdioMcpServerSpec

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class CursorLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None


def _sdk_mcp_config(spec: McpServerSpec, sdk):  # noqa: ANN001, ANN202
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
        del max_tokens, timeout_s
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
            run = self._agent.send(body)
            # Agent.send() returns a Run; block for the terminal RunResult.
            result = run.wait() if hasattr(run, "wait") else run
            text = getattr(result, "result", None) or str(result)
            status = getattr(result, "status", "finished")
            agent_id = getattr(result, "agent_id", None) or self.agent_id
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
        except Exception as exc:
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
        with contextlib.suppress(Exception):
            self._agent.dispose()


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


class CursorBackend:
    """Run prompts through Cursor SDK (Composer)."""

    def __init__(
        self,
        *,
        default_model: str = "composer-2.5",
        default_mode: AgentMode = "agent",
        api_key: str | None = None,
    ) -> None:
        self.default_model = default_model
        self.default_mode = default_mode
        self.api_key = api_key or os.environ.get("CURSOR_API_KEY", "")

    def create_session(
        self,
        *,
        persona_name: str,
        cwd: Path,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,
        model: str | None = None,
        mode: AgentMode | None = None,
    ) -> CursorSession | _ErrorSession:
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
                sdk.AgentOptions(
                    model=selected_model,
                    api_key=self.api_key,
                    local=sdk.LocalAgentOptions(cwd=str(cwd)),
                    mcp_servers=mcp_dict or None,
                    mode=selected_mode,
                    name=f"fleet:{persona_name}",
                ),
            )
        except Exception as exc:
            return _ErrorSession(f"Agent.create failed: {exc}")
        return CursorSession(agent, default_timeout_s=900)

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        memory_limit: str = "4G",
        allowed_tools: list[str] | None = None,
        cwd: Path | None = None,
        model: str | None = None,
        mode: AgentMode | None = None,
    ) -> CursorLLMResult:
        del max_tokens, memory_limit

        if not self.api_key:
            return CursorLLMResult(
                stdout="",
                stderr="CURSOR_API_KEY is not set",
                exit_code=1,
                duration_s=0.0,
            )

        try:
            from cursor_sdk import Agent, AgentOptions, LocalAgentOptions
        except ImportError as exc:
            return CursorLLMResult(
                stdout="",
                stderr=f"cursor-sdk not installed: {exc}",
                exit_code=1,
                duration_s=0.0,
            )

        work_dir = str(cwd or Path.cwd())
        selected_model = model or self.default_model
        selected_mode = coerce_agent_mode(mode, default=self.default_mode)
        scope_note = ""
        if allowed_tools:
            scoped = [
                tool.removeprefix("path:")
                for tool in allowed_tools
                if tool.startswith("path:")
            ]
            if scoped:
                scope_note = (
                    "\n\nHard scope constraint: only modify files under these prefixes: "
                    + ", ".join(scoped)
                )
        prompt_with_scope = f"{prompt}{scope_note}" if scope_note else prompt
        t0 = time.monotonic()

        def _execute() -> CursorLLMResult:
            try:
                result = Agent.prompt(
                    prompt_with_scope,
                    AgentOptions(
                        model=selected_model,
                        mode=selected_mode,
                        api_key=self.api_key,
                        local=LocalAgentOptions(cwd=work_dir),
                    ),
                )
                duration_s = time.monotonic() - t0
                text = getattr(result, "result", None) or str(result)
                agent_id = getattr(result, "agent_id", None)
                status = getattr(result, "status", "finished")
                if status in {"error", "cancelled", "expired"}:
                    return CursorLLMResult(
                        stdout=text or "",
                        stderr=f"Cursor run status: {status}",
                        exit_code=1,
                        duration_s=duration_s,
                        agent_id=agent_id,
                    )
                # Defensive: empty result is a backend failure, not success
                if not text or not text.strip():
                    return CursorLLMResult(
                        stdout="",
                        stderr="Cursor returned empty result (likely backend timeout or resource exhaustion)",
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
            except Exception as exc:
                return CursorLLMResult(
                    stdout="",
                    stderr=str(exc),
                    exit_code=1,
                    duration_s=time.monotonic() - t0,
                )

        if timeout_s <= 0:
            return _execute()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_execute)
            try:
                return future.result(timeout=timeout_s)
            except FuturesTimeoutError:
                return CursorLLMResult(
                    stdout="",
                    stderr=f"Cursor run timed out after {timeout_s}s",
                    exit_code=1,
                    duration_s=time.monotonic() - t0,
                )
