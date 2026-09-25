"""Command Code CLI backend — headless ``cmd -p`` (LongCat / catalog models).

Auth is Command Code login (``cmd login`` → ``~/.commandcode/auth.json``).
Fleet never injects a Command Code API key. Taste is apply-only: an existing
``taste.md`` is symlinked into the worktree; learning is disabled.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.contracts.mcp import McpServerSpec
    from agent_fleet.contracts.mcp_requirement import McpRequirement

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "meituan/longcat-2.0:free"
_SESSION_RE = re.compile(r"session:\s*([0-9a-f-]{36})", re.I)
_DEFAULT_TASTE = Path("/home/evan/Documents/.commandcode/taste/taste.md")


def _find_cmd_bin() -> str:
    found = shutil.which("cmd")
    return found if found else "cmd"


def _auth_json_candidates() -> list[Path]:
    home = Path.home()
    return [
        home / ".commandcode" / "auth.json",
        Path("/home/evan/.commandcode/auth.json"),
        home / ".grok-home" / ".commandcode" / "auth.json",
    ]


def check_cmd_auth() -> tuple[bool, str, str]:
    """Probe Command Code CLI + login. Returns ``(ok, detail, fix)``."""
    bin_path = _find_cmd_bin()
    if not Path(bin_path).exists() and shutil.which(bin_path) is None:
        return (
            False,
            "cmd binary not found",
            "npm i -g command-code && cmd login",
        )
    for auth in _auth_json_candidates():
        if not auth.is_file():
            continue
        try:
            raw = auth.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            data = json.loads(raw)
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data:
            return True, f"authenticated ({auth})", ""
    return (
        False,
        "Command Code auth.json missing or empty",
        "run `cmd login`",
    )


def apply_cmd_taste(work_dir: str, taste_src: str | Path | None) -> Path | None:
    """Symlink an existing taste file into HOME and the worktree. Never writes learnings."""
    src = Path(taste_src).expanduser() if taste_src else _DEFAULT_TASTE
    if not src.is_file() or src.stat().st_size == 0:
        logger.debug("cmd taste: no file at %s", src)
        return None
    src = src.resolve()
    dests = [
        Path.home() / ".commandcode" / "taste" / "taste.md",
        Path(work_dir) / ".commandcode" / "taste" / "taste.md",
    ]
    for dest in dests:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        dest.symlink_to(src)
    return src


def _parse_cmd_stream(stdout: str, stderr: str) -> tuple[str, str | None, dict[str, int] | None]:
    """Return (final_text, session_id, usage) from cmd JSON NDJSON + verbose stderr.

    ``final_text`` is the ``result`` event's ``finalText`` when the run produced
    one, else the assistant text accumulated from the stream. A turn-capped run
    (exit 8) ends in a ``result`` event with an **empty** ``finalText``; treating
    that as the answer erased the assistant text the agent had already written,
    so a review that did find blockers reported nothing at all.
    """
    session_id: str | None = None
    m = _SESSION_RE.search(stderr or "")
    if m:
        session_id = m.group(1)
    final = ""
    reported = ""
    assistant_text: list[str] = []
    usage: dict[str, int] | None = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        sm = _SESSION_RE.match(line)
        if sm:
            session_id = sm.group(1)
            continue
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result":
            session_id = obj.get("sessionId") or session_id
            reported = str(obj.get("finalText") or "")
            raw_usage = obj.get("usage")
            if isinstance(raw_usage, dict):
                usage = {}
                mapping = (
                    ("inputTokens", "input_tokens"),
                    ("outputTokens", "output_tokens"),
                    ("cacheReadTokens", "cache_read_tokens"),
                    ("cacheWriteTokens", "cache_write_tokens"),
                )
                for src, dst in mapping:
                    val = raw_usage.get(src)
                    if val is None:
                        continue
                    try:
                        usage[dst] = int(val)
                    except TypeError, ValueError:
                        continue
                if not usage:
                    usage = None
            ev = obj.get("event")
            if isinstance(ev, dict) and ev.get("sessionId"):
                session_id = ev["sessionId"]
        ev = obj.get("event") if obj.get("type") == "event" else None
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "run_start" and ev.get("sessionId"):
            session_id = ev["sessionId"]
        if ev.get("type") == "assistant":
            text = ev.get("text")
            if isinstance(text, str) and text.strip():
                assistant_text.append(text)
    final = reported.strip() or "\n".join(assistant_text).strip() or (stdout or "").strip()
    return final, session_id, usage


def call_cmd(
    prompt: str,
    *,
    work_dir: str,
    timeout: int = 1800,
    model: str = DEFAULT_MODEL,
    cmd_bin: str | None = None,
    mode: str | None = None,
    session_id: str | None = None,
    resume: bool = False,
    taste_src: str | Path | None = None,
    max_turns: int = 80,
) -> tuple[str, str | None, dict[str, int] | None, int]:
    """Run ``cmd -p``. Returns (final_text, session_id, usage, exit_code).

    Exit 0 and 8 (turn cap with partial answer) are both returned, not raised.
    Other non-zero exits raise RuntimeError.
    """
    bin_path = cmd_bin or _find_cmd_bin()
    apply_cmd_taste(work_dir, taste_src)
    cmd = [
        bin_path,
        "-p",
        "--skip-onboarding",
        "--trust",
        "--no-auto-update",
        "--config",
        "taste-learning=false",
        "--max-turns",
        str(max_turns),
        "--verbose",
        "--output-format",
        "json",
        "-m",
        model,
        "-n",
        "fleet-cmd",
    ]
    if mode == "plan":
        cmd.extend(["--permission-mode", "plan"])
    else:
        cmd.append("--yolo")
    if session_id and resume:
        cmd.extend(["--resume", session_id])

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout if timeout > 0 else 1800,
        cwd=work_dir,
        env=os.environ.copy(),
        check=False,
    )
    final, parsed_session, usage = _parse_cmd_stream(result.stdout or "", result.stderr or "")
    code = result.returncode
    if code not in (0, 8):
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"cmd failed (exit {code}): {err[:500]}")
    return final, parsed_session or session_id, usage, code


@dataclass(frozen=True)
class CmdLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None
    usage: dict[str, int] | None = None


class CmdSession:
    """Durable cmd session: first send is fresh; later sends ``--resume``."""

    def __init__(
        self,
        *,
        cmd_bin: str,
        model: str,
        cwd: Path,
        mode: str | None = None,
        taste_src: str | None = None,
    ) -> None:
        self._cmd_bin = cmd_bin
        self._model = model
        self._cwd = cwd
        self._mode = mode
        self._taste_src = taste_src
        self._session_id: str | None = None
        self._started = False
        self.agent_id: str | None = None

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> CmdLLMResult:
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
            stdout, session_id, usage, code = call_cmd(
                prompt_with_scope,
                work_dir=str(self._cwd),
                timeout=timeout_s if timeout_s > 0 else 1800,
                model=self._model,
                cmd_bin=self._cmd_bin,
                mode=self._mode,
                session_id=self._session_id,
                resume=self._started,
                taste_src=self._taste_src,
            )
            self._started = True
            if session_id:
                self._session_id = session_id
                self.agent_id = session_id
            return CmdLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=0 if code in (0, 8) else code,
                duration_s=time.monotonic() - t0,
                agent_id=self.agent_id,
                usage=usage,
            )
        except Exception as exc:
            return CmdLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
                agent_id=self.agent_id,
            )

    def dispose(self) -> None:
        """Session lives on disk under ~/.commandcode/projects."""


class _CmdErrorSession:
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
    ) -> CmdLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools, expect_mcp_tools, mcp_requirement
        return CmdLLMResult(stdout="", stderr=self._message, exit_code=1, duration_s=0.0)

    def dispose(self) -> None:
        pass


class CmdBackend:
    """Run prompts through Command Code CLI (``cmd -p``)."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        cmd_bin: str | None = None,
        default_mode: str | None = None,
        cmd_taste: str | None = None,
    ) -> None:
        self.model = model
        self.cmd_bin = str(Path(cmd_bin).expanduser()) if cmd_bin else _find_cmd_bin()
        self.default_mode = default_mode
        self.cmd_taste = cmd_taste

    def create_session(
        self,
        *,
        persona_name: str,  # noqa: ARG002
        cwd: Path,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,  # noqa: ARG002
        model: str | None = None,
        mode: AgentMode | str | None = None,
        session_id: str | None = None,  # noqa: ARG002 (no resume path for this backend)
    ) -> CmdSession | _CmdErrorSession:
        ok, detail, fix = check_cmd_auth()
        if not ok:
            msg = detail if not fix else f"{detail}; {fix}"
            return _CmdErrorSession(msg)
        return CmdSession(
            cmd_bin=self.cmd_bin,
            model=model or self.model,
            cwd=cwd,
            mode=mode or self.default_mode,
            taste_src=self.cmd_taste,
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
    ) -> CmdLLMResult:
        del max_tokens, memory_limit
        ok, detail, fix = check_cmd_auth()
        if not ok:
            msg = detail if not fix else f"{detail}; {fix}"
            return CmdLLMResult(stdout="", stderr=msg, exit_code=1, duration_s=0.0)

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
            stdout, session_id, usage, code = call_cmd(
                prompt_with_scope,
                work_dir=work_dir,
                timeout=timeout_s if timeout_s > 0 else 1800,
                model=selected_model,
                cmd_bin=self.cmd_bin,
                mode=selected_mode,
                taste_src=self.cmd_taste,
            )
            return CmdLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=0 if code in (0, 8) else code,
                duration_s=time.monotonic() - t0,
                agent_id=session_id,
                usage=usage,
            )
        except Exception as exc:
            return CmdLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
            )
