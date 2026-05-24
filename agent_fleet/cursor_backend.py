"""Cursor SDK backend implementing the fleet LLMBackend protocol."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from agent_fleet.agent_mode import AgentMode, coerce_agent_mode
from agent_fleet.contracts.mcp import McpServerSpec, StdioMcpServerSpec
from agent_fleet.contracts.mcp_requirement import McpRequirement
from agent_fleet.observability.context import get_run_log

logger = logging.getLogger(__name__)
mcp_logger = logging.getLogger("agent_fleet.mcp")


@dataclass(frozen=True)
class CursorLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None
    mcp_tool_calls: tuple[str, ...] = field(default_factory=tuple)


def _format_mcp_tool_label(args: Mapping[str, Any] | None) -> str | None:
    if not args:
        return None
    provider = args.get("providerIdentifier") or args.get("provider")
    tool_name = args.get("toolName") or args.get("tool")
    if provider and tool_name:
        return f"{provider}.{tool_name}"
    if tool_name:
        return str(tool_name)
    return None


def _consume_run_events(
    run,  # noqa: ANN001
    *,
    expected_mcp_servers: frozenset[str] | None = None,
    warn_if_unused: bool = False,
) -> tuple[str, ...]:
    """Drain a Run event stream and log MCP tool invocations."""
    labels: list[str] = []
    seen_running: set[str] = set()

    events = getattr(run, "events", None)
    if not callable(events):
        if hasattr(run, "wait"):
            run.wait()
        return tuple(labels)

    for event in events():
        msg = getattr(event, "sdk_message", None)
        if msg is None or getattr(msg, "type", None) != "tool_call":
            continue
        if getattr(msg, "name", None) != "mcp":
            continue

        call_args = getattr(msg, "args", None) or {}
        label = _format_mcp_tool_label(call_args)
        if not label:
            continue

        status = getattr(msg, "status", "")
        call_id = getattr(msg, "call_id", "") or label
        tool_args = call_args.get("args") if isinstance(call_args, Mapping) else None
        args_preview = ""
        if isinstance(tool_args, Mapping) and tool_args:
            try:
                args_preview = json.dumps(tool_args, default=str)[:240]
            except TypeError:
                args_preview = str(tool_args)[:240]

        if status == "running" and call_id not in seen_running:
            seen_running.add(call_id)
            labels.append(label)
            mcp_logger.info(
                "MCP tool call started: %s args=%s agent_id=%s run_id=%s",
                label,
                args_preview or "{}",
                getattr(msg, "agent_id", None),
                getattr(msg, "run_id", None),
            )
            run_log = get_run_log()
            if run_log is not None:
                run_log.mcp_tool(
                    action="start",
                    tool=label,
                    args=args_preview or "{}",
                    agent_id=getattr(msg, "agent_id", None),
                )
        elif status == "completed":
            mcp_logger.info(
                "MCP tool call completed: %s agent_id=%s run_id=%s",
                label,
                getattr(msg, "agent_id", None),
                getattr(msg, "run_id", None),
            )
            run_log = get_run_log()
            if run_log is not None:
                run_log.mcp_tool(
                    action="complete",
                    tool=label,
                    agent_id=getattr(msg, "agent_id", None),
                )

    if hasattr(run, "wait"):
        run.wait()

    if warn_if_unused and expected_mcp_servers and not labels:
        mcp_logger.warning(
            "MCP servers attached (%s) but no MCP tool calls observed in this send()",
            ", ".join(sorted(expected_mcp_servers)),
        )
    elif labels:
        mcp_logger.info(
            "MCP tool summary: %s",
            ", ".join(f"{name}×{labels.count(name)}" for name in dict.fromkeys(labels)),
        )

    return tuple(labels)


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
        mcp_servers: frozenset[str] | None = None,
    ) -> None:
        self._agent = agent
        self._default_timeout_s = default_timeout_s
        self._mcp_servers = mcp_servers or frozenset()
        self._disposed = False
        self.agent_id: str | None = getattr(agent, "agent_id", None)
        if "playwright" in self._mcp_servers:
            from agent_fleet.memory import PlaywrightSessionRegistry

            PlaywrightSessionRegistry.register()
        if self._mcp_servers:
            mcp_logger.info(
                "MCP session created agent_id=%s servers=%s",
                self.agent_id,
                ", ".join(sorted(self._mcp_servers)),
            )

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> CursorLLMResult:
        del max_tokens, timeout_s
        requirement = mcp_requirement or (
            McpRequirement.playwright_visual() if expect_mcp_tools else McpRequirement.none()
        )
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
            mcp_tool_calls = _consume_run_events(
                run,
                expected_mcp_servers=self._mcp_servers or None,
                warn_if_unused=requirement.required,
            )
            result = getattr(run, "_terminal_result", None)
            if result is None and hasattr(run, "wait"):
                result = run.wait()
            elif result is None:
                result = run
            text = getattr(result, "result", None) or str(result)
            status = getattr(result, "status", "finished")
            agent_id = getattr(result, "agent_id", None) or self.agent_id
            duration_s = time.monotonic() - t0
            check = requirement.check(mcp_tool_calls)
            run_log = get_run_log()
            if run_log is not None:
                run_log.mcp_requirement(
                    passed=check.passed,
                    reason=check.reason,
                    requirement=requirement.to_dict(),
                    observed_tools=list(check.observed_tools),
                    missing_tools=list(check.missing_tools),
                )
            if not check.passed:
                msg = (
                    f"MCP requirement failed: {check.reason} "
                    f"(attached servers: {', '.join(sorted(self._mcp_servers))})"
                )
                if check.missing_tools:
                    msg += f"; missing tools: {', '.join(check.missing_tools)}"
                mcp_logger.error(msg)
                return CursorLLMResult(
                    stdout=text or "",
                    stderr=msg,
                    exit_code=1,
                    duration_s=duration_s,
                    agent_id=agent_id,
                    mcp_tool_calls=mcp_tool_calls,
                )

            if status in {"error", "cancelled", "expired"}:
                return CursorLLMResult(
                    stdout=text or "",
                    stderr=f"Cursor send status: {status}",
                    exit_code=1,
                    duration_s=duration_s,
                    agent_id=agent_id,
                    mcp_tool_calls=mcp_tool_calls,
                )
            return CursorLLMResult(
                stdout=text or "",
                stderr="",
                exit_code=0,
                duration_s=duration_s,
                agent_id=agent_id,
                mcp_tool_calls=mcp_tool_calls,
            )
        except Exception as exc:
            logger.exception("CursorSession.send failed")
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
        servers = ", ".join(sorted(self._mcp_servers)) if self._mcp_servers else "none"
        has_playwright = "playwright" in self._mcp_servers
        run_log = get_run_log()
        if run_log is not None:
            run_log.emit(
                "mcp.session.dispose.start",
                data={"agent_id": self.agent_id, "servers": servers},
            )
        mcp_logger.info(
            "MCP session disposing agent_id=%s servers=%s",
            self.agent_id,
            servers,
        )
        before_count = 0
        if has_playwright:
            from agent_fleet.memory import count_playwright_mcp_processes

            before_count = count_playwright_mcp_processes()
        with contextlib.suppress(Exception):
            self._agent.dispose()
        cleanup_result = None
        if has_playwright:
            from agent_fleet.memory import (
                PlaywrightSessionRegistry,
                cleanup_playwright_mcp_processes,
            )

            remaining_sessions = PlaywrightSessionRegistry.unregister()
            cleanup_result = cleanup_playwright_mcp_processes(
                baseline=before_count,
                wait_s=10.0,
                poll_interval_s=0.5,
                force_kill=remaining_sessions == 0,
            )
            msg_level = logging.INFO if cleanup_result.cleaned else logging.WARNING
            if cleanup_result.force_killed:
                mcp_logger.log(
                    msg_level,
                    "MCP session disposed agent_id=%s playwright cleanup "
                    "before=%s after=%s waited_s=%s force_killed=%s",
                    self.agent_id,
                    cleanup_result.before,
                    cleanup_result.after,
                    cleanup_result.waited_s,
                    list(cleanup_result.force_killed),
                )
            else:
                mcp_logger.log(
                    msg_level,
                    "MCP session disposed agent_id=%s playwright cleanup "
                    "before=%s after=%s waited_s=%s",
                    self.agent_id,
                    cleanup_result.before,
                    cleanup_result.after,
                    cleanup_result.waited_s,
                )
            if run_log is not None:
                run_log.emit(
                    "mcp.session.dispose.end",
                    level="info" if cleanup_result.cleaned else "warning",
                    data={
                        "agent_id": self.agent_id,
                        "playwright_mcp_processes_before": cleanup_result.before,
                        "playwright_mcp_processes_after": cleanup_result.after,
                        "waited_s": cleanup_result.waited_s,
                        "force_killed": list(cleanup_result.force_killed),
                        "cleaned": cleanup_result.cleaned,
                        "remaining_playwright_sessions": remaining_sessions,
                    },
                )
        elif self._mcp_servers:
            mcp_logger.info("MCP session disposed agent_id=%s", self.agent_id)
            if run_log is not None:
                run_log.emit(
                    "mcp.session.dispose.end",
                    data={"agent_id": self.agent_id, "servers": servers},
                )


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

    # Cursor SDK uses a singleton bridge process that is NOT thread-safe.
    # Concurrent Agent.prompt() calls from multiple threads race the gRPC
    # bridge and produce "internal: internal error".  Serialize access.
    _prompt_lock = threading.Lock()

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
        return CursorSession(
            agent,
            default_timeout_s=900,
            mcp_servers=frozenset(mcp_dict),
        )

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
                with CursorBackend._prompt_lock:
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
