"""Grok Build CLI backend — subscription-only auth via ``grok login``.

Uses the headless ``grok`` binary (``--prompt-file``, ``--yolo`` / plan mode,
session ``-s``/``-r``). Authentication is subscription OIDC stored in
``~/.grok/auth.json`` after ``grok login``. Fleet never injects ``XAI_API_KEY``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.contracts.mcp import McpServerSpec
    from agent_fleet.contracts.mcp_requirement import McpRequirement


DEFAULT_MODEL = "grok-4.5"
AUTH_JSON = Path("~/.grok/auth.json").expanduser()
LOCAL_BIN = Path("~/.grok/bin/grok").expanduser()


def _find_grok_bin() -> str:
    for candidate in (shutil.which("grok"), str(LOCAL_BIN)):
        if candidate and Path(candidate).exists():
            return candidate
    return "grok"


def check_grok_auth() -> tuple[bool, str, str]:
    """Probe Grok Build subscription auth (binary + ``~/.grok/auth.json``).

    Returns ``(ok, detail, fix)``. Does **not** require ``XAI_API_KEY``.
    """
    bin_path = _find_grok_bin()
    if not Path(bin_path).exists() and shutil.which(bin_path) is None:
        return (
            False,
            "grok binary not found",
            "Install Grok Build CLI (https://x.ai/cli) or set grok_bin in fleet.yaml",
        )

    if not AUTH_JSON.exists():
        return (
            False,
            f"{AUTH_JSON} missing",
            "run `grok login` (SuperGrok / X Premium+)",
        )

    try:
        raw = AUTH_JSON.read_text(encoding="utf-8").strip()
        if not raw:
            return (
                False,
                f"{AUTH_JSON} is empty",
                "run `grok login` (SuperGrok / X Premium+)",
            )
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return (
            False,
            f"{AUTH_JSON} invalid: {exc}",
            "run `grok login` (SuperGrok / X Premium+)",
        )

    if not isinstance(data, dict) or not data:
        return (
            False,
            f"{AUTH_JSON} is not a non-empty JSON object",
            "run `grok login` (SuperGrok / X Premium+)",
        )

    return True, f"authenticated ({AUTH_JSON})", ""


def call_grok(
    prompt: str,
    *,
    work_dir: str,
    timeout: int = 720,
    model: str = DEFAULT_MODEL,
    grok_bin: str | None = None,
    mode: str | None = None,
    session_id: str | None = None,
    resume: bool = False,
) -> str:
    """Run ``grok`` headless with a prompt file. Returns plain-text stdout.

    Does **not** set ``XAI_API_KEY`` — relies on subscription auth in
    ``~/.grok/auth.json``.
    """
    bin_path = grok_bin or _find_grok_bin()
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".prompt.txt",
        delete=False,
    ) as handle:
        handle.write(prompt)
        prompt_path = handle.name

    try:
        cmd = [
            bin_path,
            "--no-auto-update",
            "--cwd",
            work_dir,
            "--prompt-file",
            prompt_path,
            "--output-format",
            "plain",
            "-m",
            model,
        ]
        if mode == "plan":
            cmd.extend(["--permission-mode", "plan"])
        else:
            cmd.append("--yolo")

        if session_id:
            if resume:
                cmd.extend(["-r", session_id])
            else:
                cmd.extend(["-s", session_id])

        # Do not inject XAI_API_KEY; subscription auth uses ~/.grok/auth.json.
        env = os.environ.copy()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout if timeout > 0 else 720,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"grok failed (exit {result.returncode}): {err[:500]}"
            )
        return (result.stdout or "").strip()
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass


@dataclass(frozen=True)
class GrokLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None
    usage: dict[str, int] | None = None


class GrokSession:
    """Durable Grok session: first send uses ``-s`` UUID, later sends ``-r``."""

    def __init__(
        self,
        *,
        grok_bin: str,
        model: str,
        cwd: Path,
        mode: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._grok_bin = grok_bin
        self._model = model
        self._cwd = cwd
        self._mode = mode
        self._session_id = session_id or str(uuid.uuid4())
        self._started = False
        self.agent_id: str | None = self._session_id

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> GrokLLMResult:
        del max_tokens, expect_mcp_tools, mcp_requirement

        scope_note = ""
        if allowed_tools:
            scoped = [
                tool.removeprefix("path:") for tool in allowed_tools if tool.startswith("path:")
            ]
            if scoped:
                scope_note = (
                    "\n\nHard scope constraint: only modify files under these prefixes: "
                    + ", ".join(scoped)
                )
        prompt_with_scope = f"{prompt}{scope_note}" if scope_note else prompt

        t0 = time.monotonic()
        try:
            stdout = call_grok(
                prompt_with_scope,
                work_dir=str(self._cwd),
                timeout=timeout_s if timeout_s > 0 else 720,
                model=self._model,
                grok_bin=self._grok_bin,
                mode=self._mode,
                session_id=self._session_id,
                resume=self._started,
            )
            self._started = True
            return GrokLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=0,
                duration_s=time.monotonic() - t0,
                agent_id=self._session_id,
            )
        except Exception as exc:
            return GrokLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
                agent_id=self._session_id,
            )

    def dispose(self) -> None:
        """No persistent process to tear down — session lives on disk under ~/.grok."""


class _GrokErrorSession:
    """Stub session that always fails — used when auth is missing."""

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
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> GrokLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools, expect_mcp_tools, mcp_requirement
        return GrokLLMResult(stdout="", stderr=self._message, exit_code=1, duration_s=0.0)

    def dispose(self) -> None:
        pass


class GrokBackend:
    """Run prompts through Grok Build CLI (subscription / ``grok login``)."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        grok_bin: str | None = None,
        default_mode: str | None = None,
    ) -> None:
        self.model = model
        if grok_bin:
            self.grok_bin = str(Path(grok_bin).expanduser())
        else:
            self.grok_bin = _find_grok_bin()
        self.default_mode = default_mode

    def create_session(
        self,
        *,
        persona_name: str,  # noqa: ARG002
        cwd: Path,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,  # noqa: ARG002
        model: str | None = None,
        mode: AgentMode | str | None = None,
    ) -> GrokSession | _GrokErrorSession:
        """Create a durable headless Grok session (UUID via ``-s``, resume via ``-r``)."""
        ok, detail, fix = check_grok_auth()
        if not ok:
            msg = detail if not fix else f"{detail}; {fix}"
            return _GrokErrorSession(msg)
        return GrokSession(
            grok_bin=self.grok_bin,
            model=model or self.model,
            cwd=cwd,
            mode=mode or self.default_mode,
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
        mode: str | None = None,
    ) -> GrokLLMResult:
        del max_tokens, memory_limit

        ok, detail, fix = check_grok_auth()
        if not ok:
            msg = detail if not fix else f"{detail}; {fix}"
            return GrokLLMResult(
                stdout="",
                stderr=msg,
                exit_code=1,
                duration_s=0.0,
            )

        work_dir = str(cwd or Path.cwd())
        selected_model = model or self.model
        selected_mode = mode or self.default_mode
        scope_note = ""
        if allowed_tools:
            scoped = [
                tool.removeprefix("path:") for tool in allowed_tools if tool.startswith("path:")
            ]
            if scoped:
                scope_note = (
                    "\n\nHard scope constraint: only modify files under these prefixes: "
                    + ", ".join(scoped)
                )
        prompt_with_scope = f"{prompt}{scope_note}" if scope_note else prompt
        t0 = time.monotonic()
        try:
            stdout = call_grok(
                prompt_with_scope,
                work_dir=work_dir,
                timeout=timeout_s if timeout_s > 0 else 720,
                model=selected_model,
                grok_bin=self.grok_bin,
                mode=selected_mode,
            )
            return GrokLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=0,
                duration_s=time.monotonic() - t0,
            )
        except Exception as exc:
            return GrokLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
            )
