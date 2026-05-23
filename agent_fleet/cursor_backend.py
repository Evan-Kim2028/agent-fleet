"""Cursor SDK backend implementing the fleet LLMBackend protocol."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CursorLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None


class CursorBackend:
    """Run prompts through Cursor SDK (Composer)."""

    def __init__(
        self,
        *,
        default_model: str = "composer-2.5",
        default_mode: str = "agent",
        api_key: str | None = None,
    ) -> None:
        self.default_model = default_model
        self.default_mode = default_mode
        self.api_key = api_key or os.environ.get("CURSOR_API_KEY", "")

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
        mode: str | None = None,
    ) -> CursorLLMResult:
        del max_tokens, memory_limit, allowed_tools

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
        selected_mode = mode or self.default_mode
        t0 = time.monotonic()

        def _execute() -> CursorLLMResult:
            try:
                result = Agent.prompt(
                    prompt,
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
