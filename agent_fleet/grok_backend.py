"""Grok Build CLI backend — subscription-only auth via ``grok login``.

Uses the headless ``grok`` binary (``--prompt-file``, ``--yolo`` / plan mode,
session ``-s``/``-r``). Authentication is subscription OIDC stored in
``~/.grok/auth.json`` after ``grok login``. Fleet never injects ``XAI_API_KEY``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.observability.context import get_run_context, get_run_log

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.contracts.mcp import McpServerSpec
    from agent_fleet.contracts.mcp_requirement import McpRequirement


logger = logging.getLogger(__name__)

DEFAULT_MODEL = "grok-4.5"
AUTH_JSON = Path("~/.grok/auth.json").expanduser()
LOCAL_BIN = Path("~/.grok/bin/grok").expanduser()

# Root of the Grok CLI's per-session usage logs. A subdirectory tree of
# <url-encoded-cwd>/<session-uuid>/updates.jsonl. Overridable (module attr)
# so tests can point it at a tmp_path fixture; production code always reads
# the module attribute at call time so monkeypatching works.
GROK_SESSIONS_ROOT = Path("~/.grok/sessions").expanduser()

# Grok's usage object is cumulative per session. Upstream keys are camelCase;
# normalize to the snake_case fields the other backends (cursor/openrouter)
# already feed into RunLog.llm_usage.
_GROK_USAGE_KEY_MAP = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cachedReadTokens": "cache_read_tokens",
}

# session_id -> last-seen cumulative usage (normalized). Used to emit deltas
# instead of double-counting the cumulative totals on every read.
_last_session_usage: dict[str, dict[str, int]] = {}


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


# ---------------------------------------------------------------------------
# Token usage: read Grok CLI's per-session updates.jsonl, diff against the
# last-seen cumulative totals, and emit an llm.usage RunLog entry (mirrors
# cursor_backend._log_llm_usage / openrouter_backend._log_llm_usage).
# ---------------------------------------------------------------------------


def _encode_cwd(work_dir: str) -> str:
    """Percent-encode an absolute cwd the way the Grok CLI names session dirs."""
    return urllib.parse.quote(str(work_dir), safe="")


def _normalize_grok_usage(raw: Mapping[str, Any] | None) -> dict[str, int] | None:
    if not raw:
        return None
    out: dict[str, int] = {}
    for src, dst in _GROK_USAGE_KEY_MAP.items():
        val = raw.get(src)
        if val is None:
            continue
        try:
            out[dst] = int(val)
        except TypeError, ValueError:
            continue
    return out or None


def _updates_jsonl_path(work_dir: str, session_id: str) -> Path:
    return GROK_SESSIONS_ROOT / _encode_cwd(work_dir) / session_id / "updates.jsonl"


def _read_cumulative_usage(work_dir: str, session_id: str) -> dict[str, int] | None:
    """Read the last "usage" object from a session's updates.jsonl (cumulative).

    Never raises: missing files, unreadable files, and corrupt/partial JSON
    lines are all treated as "no usage available yet".
    """
    path = _updates_jsonl_path(work_dir, session_id)
    if not path.exists():
        return None
    last_raw: dict[str, Any] | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    usage = obj.get("usage")
                    if isinstance(usage, dict):
                        last_raw = usage
    except OSError as exc:
        logger.debug("grok usage: failed reading %s: %s", path, exc)
        return None
    return _normalize_grok_usage(last_raw)


def _parse_created_at(value: object) -> float | None:
    """Best-effort epoch-seconds parse of a summary.json created_at field."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Grok CLI timestamps observed as millisecond epoch; values above this
        # threshold are almost certainly ms, not seconds.
        return value / 1000.0 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _find_session_id_by_cwd(work_dir: str, *, since_ts: float) -> str | None:
    """Best-effort session lookup for one-shot (no explicit session_id) calls.

    Matches ``summary.json``'s ``info.cwd`` against *work_dir* and picks the
    single newest session created after *since_ts*. Returns ``None`` (silent
    skip) when there is no unambiguous match — never raises.
    """
    cwd_dir = GROK_SESSIONS_ROOT / _encode_cwd(work_dir)
    if not cwd_dir.is_dir():
        return None
    candidates: list[tuple[float, str]] = []
    try:
        for entry in cwd_dir.iterdir():
            if not entry.is_dir():
                continue
            summary_path = entry / "summary.json"
            if not summary_path.exists():
                continue
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError, UnicodeDecodeError:
                continue
            if not isinstance(summary, dict):
                continue
            info = summary.get("info")
            if not isinstance(info, dict) or info.get("cwd") != work_dir:
                continue
            created_at = _parse_created_at(
                info.get("createdAt") or info.get("created_at") or summary.get("createdAt")
            )
            if created_at is None or created_at < since_ts:
                continue
            candidates.append((created_at, entry.name))
    except OSError as exc:
        logger.debug("grok usage: failed scanning %s: %s", cwd_dir, exc)
        return None
    if not candidates:
        return None
    newest_ts = max(ts for ts, _sid in candidates)
    newest = [sid for ts, sid in candidates if ts == newest_ts]
    if len(newest) != 1:
        return None  # ambiguous — skip silently
    return newest[0]


# Keys accepted by RunLog.llm_usage() — anything else (e.g. our synthesized
# total_tokens) must be filtered out before splatting into that call.
_RUN_LOG_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _log_llm_usage(
    *,
    phase: str | None,
    model: str | None,
    usage: dict[str, int] | None,
    duration_s: float,
    agent_id: str | None,
) -> None:
    """Emit an llm.usage RunLog entry — mirrors cursor_backend._log_llm_usage."""
    if not usage:
        return
    run_log = get_run_log()
    if run_log is not None:
        run_log.llm_usage(
            phase=phase,
            model=model,
            duration_s=duration_s,
            agent_id=agent_id,
            **{k: int(v) for k, v in usage.items() if k in _RUN_LOG_USAGE_FIELDS},
        )


def _harvest_grok_usage(
    *,
    work_dir: str,
    session_id: str | None,
    call_started_at: float,
    phase: str | None,
    model: str | None,
    duration_s: float,
) -> dict[str, int] | None:
    """Diff this call's cumulative session usage against the last-seen totals.

    Emits an ``llm.usage`` RunLog entry for the delta (never the raw
    cumulative totals, so per-task rollups don't double-count) and returns
    that delta for :class:`GrokLLMResult`. Returns ``None`` — quietly,
    without raising — whenever usage can't be resolved (no session, no
    updates.jsonl, no usage lines yet, ambiguous one-shot lookup, ...).
    """
    try:
        resolved_session_id = session_id
        if resolved_session_id is None:
            resolved_session_id = _find_session_id_by_cwd(work_dir, since_ts=call_started_at)
        if resolved_session_id is None:
            return None

        cumulative = _read_cumulative_usage(work_dir, resolved_session_id)
        if cumulative is None:
            return None

        previous = _last_session_usage.get(resolved_session_id, {})
        delta: dict[str, int] = {
            key: max(value - previous.get(key, 0), 0) for key, value in cumulative.items()
        }
        _last_session_usage[resolved_session_id] = cumulative

        if not any(delta.values()):
            return None

        _log_llm_usage(
            phase=phase,
            model=model,
            usage=delta,
            duration_s=duration_s,
            agent_id=resolved_session_id,
        )
        # total_tokens is synthesized for callers of GrokLLMResult.usage; it is
        # NOT a RunLog.llm_usage() kwarg (filtered out in _log_llm_usage above).
        result = dict(delta)
        result["total_tokens"] = delta.get("input_tokens", 0) + delta.get("output_tokens", 0)
        return result
    except Exception as exc:  # usage harvesting must never break a run
        logger.debug("grok usage: harvest failed for session=%s: %s", session_id, exc)
        return None


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
            raise RuntimeError(f"grok failed (exit {result.returncode}): {err[:500]}")
        return (result.stdout or "").strip()
    finally:
        Path(prompt_path).unlink(missing_ok=True)


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
            duration_s = time.monotonic() - t0
            ctx = get_run_context()
            usage = _harvest_grok_usage(
                work_dir=str(self._cwd),
                session_id=self._session_id,
                call_started_at=t0,
                phase=ctx.phase if ctx is not None else None,
                model=self._model,
                duration_s=duration_s,
            )
            return GrokLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=0,
                duration_s=duration_s,
                agent_id=self._session_id,
                usage=usage,
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
        call_started_at = time.time()
        try:
            stdout = call_grok(
                prompt_with_scope,
                work_dir=work_dir,
                timeout=timeout_s if timeout_s > 0 else 720,
                model=selected_model,
                grok_bin=self.grok_bin,
                mode=selected_mode,
            )
            duration_s = time.monotonic() - t0
            ctx = get_run_context()
            # No explicit session_id was passed to call_grok for one-shot
            # runs — try to resolve the session Grok created by matching cwd
            # + recency in ~/.grok/sessions (see _find_session_id_by_cwd).
            usage = _harvest_grok_usage(
                work_dir=work_dir,
                session_id=None,
                call_started_at=call_started_at,
                phase=ctx.phase if ctx is not None else None,
                model=selected_model,
                duration_s=duration_s,
            )
            return GrokLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=0,
                duration_s=duration_s,
                usage=usage,
            )
        except Exception as exc:
            return GrokLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
            )
