"""OpenRouter backend — optional OpenAI-compatible HTTP backend.

Talks to OpenRouter's ``/api/v1/chat/completions`` endpoint using only the
Python standard library (``urllib.request``), mirroring the dependency-light
approach of ``agent_fleet/pr_review/github.py``. No new runtime dependency is
added.

The default model is ``tencent/hy3:free`` — a 295B MoE reasoning model exposed
via OpenRouter. Any OpenRouter model slug (``provider/model[:variant]``) can be
pinned via ``default_model`` or per-persona ``model``.

Unlike the Cursor backend there is no ``fast`` tier to pin: the model string is
passed through to OpenRouter unchanged.

Tool calling: the backend implements ``SessionCapableBackend`` —
``create_session()`` returns an ``OpenRouterSession`` that drives a standard
OpenAI-compatible tool-calling loop (read_file, write_file, run_command,
list_files). The model calls tools, we execute them locally, feed results
back, and loop until ``finish_reason: "stop"``. This lets the OpenRouter
backend actually edit files and produce PRs, mirroring what the Cursor SDK
gives us for free.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_fleet.observability.context import get_run_context, get_run_log

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agent_fleet.config import McpServerSpec
    from agent_fleet.hooks import AgentMode, McpRequirement

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "tencent/hy3:free"

# Cap the tool-use loop so a misbehaving model can't run forever.
_MAX_TOOL_ITERATIONS = 25
# Timeout for run_command tool calls.
_COMMAND_TIMEOUT_S = 60


# ---------------------------------------------------------------------------
# Observability: normalize OpenRouter usage → RunLog fields
# ---------------------------------------------------------------------------


def _normalize_openrouter_usage(raw: dict[str, Any] | None) -> dict[str, int] | None:
    """Convert OpenRouter usage dict to RunLog snake_case fields.

    OpenRouter returns ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
    ``cost``, and nested ``prompt_tokens_details.cached_tokens`` (prompt cache hits).
    The RunLog expects ``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
    ``cache_write_tokens``.
    """
    if not raw:
        return None
    prompt_details = raw.get("prompt_tokens_details") or {}
    result: dict[str, int] = {
        "input_tokens": int(raw.get("prompt_tokens", 0) or 0),
        "output_tokens": int(raw.get("completion_tokens", 0) or 0),
        "cache_read_tokens": int(prompt_details.get("cached_tokens", 0) or 0),
        "cache_write_tokens": 0,  # OpenRouter doesn't report cache writes
    }
    return result if any(result.values()) else None


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
            **{k: int(v) for k, v in usage.items()},
        )


# ---------------------------------------------------------------------------
# Tool definitions for the agentic session
# ---------------------------------------------------------------------------

_FILE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Path is relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write or overwrite a file. Creates parent directories automatically. "
                "Path is relative to the workspace root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "File content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command in the workspace directory. "
                f"Returns stdout and stderr. Times out after {_COMMAND_TIMEOUT_S}s."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in a path (relative to workspace root).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path (default: '.')",
                        "default": ".",
                    },
                },
                "required": [],
            },
        },
    },
]


def _safe_resolve(path_str: str, cwd: Path) -> Path | None:
    """Resolve *path_str* against *cwd*, rejecting traversal outside cwd."""
    target = (cwd / path_str).resolve()
    try:
        target.relative_to(cwd.resolve())
    except ValueError:
        return None
    return target


def _is_within_scope(path: Path, scope_prefixes: list[str], cwd: Path) -> bool:
    """Check if *path* (relative to *cwd*) starts with any of the allowed prefixes.

    A prefix of ``"."`` or ``""`` means "the entire workspace" — matches anything.
    """
    if not scope_prefixes:
        return True
    try:
        rel = path.relative_to(cwd.resolve())
    except ValueError:
        return False
    rel_str = str(rel).replace("\\", "/")
    for prefix in scope_prefixes:
        normalized = prefix.rstrip("/")
        # "." or "" means the workspace root — matches everything.
        if normalized in ("", "."):
            return True
        if rel_str.startswith(normalized):
            return True
    return False


def _execute_tool(
    name: str,
    args: dict[str, Any],
    *,
    cwd: Path,
    scope_prefixes: list[str],
) -> str:
    """Execute a tool call and return a JSON-serialized result string.

    *scope_prefixes* restricts where ``write_file`` may create/modify files.
    ``read_file`` and ``list_files`` can read anywhere under *cwd*.
    """
    if name == "read_file":
        path = _safe_resolve(str(args.get("path", "")), cwd)
        if path is None or not path.is_file():
            return json.dumps({"error": f"File not found or outside workspace: {args.get('path')}"})
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            return json.dumps({"content": content[:50000]})  # cap at 50k chars
        except OSError as exc:
            return json.dumps({"error": str(exc)})

    if name == "write_file":
        path = _safe_resolve(str(args.get("path", "")), cwd)
        if path is None:
            return json.dumps({"error": f"Path outside workspace: {args.get('path')}"})
        if not _is_within_scope(path, scope_prefixes, cwd):
            return json.dumps({"error": f"Path outside scope: {args.get('path')}"})
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args.get("content", "")), encoding="utf-8")
            return json.dumps({"ok": True, "path": str(path.relative_to(cwd.resolve()))})
        except OSError as exc:
            return json.dumps({"error": str(exc)})

    if name == "run_command":
        command = str(args.get("command", ""))
        if not command:
            return json.dumps({"error": "Empty command"})
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_S,
            )
            return json.dumps(
                {
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout[:20000],
                    "stderr": proc.stderr[:10000],
                }
            )
        except subprocess.TimeoutExpired:
            return json.dumps({"error": f"Command timed out after {_COMMAND_TIMEOUT_S}s"})
        except OSError as exc:
            return json.dumps({"error": str(exc)})

    if name == "list_files":
        rel_path = str(args.get("path", "."))
        path = _safe_resolve(rel_path, cwd)
        if path is None or not path.is_dir():
            return json.dumps({"error": f"Directory not found or outside workspace: {rel_path}"})
        try:
            entries = sorted(
                {"type": "dir" if p.is_dir() else "file", "name": p.name}
                for p in path.iterdir()
                if not p.name.startswith(".git")
            )
            return json.dumps({"entries": entries[:500]})
        except OSError as exc:
            return json.dumps({"error": str(exc)})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ---------------------------------------------------------------------------
# Low-level HTTP call (returns full parsed response)
# ---------------------------------------------------------------------------


def _call_openrouter_raw(
    messages: list[dict[str, Any]],
    *,
    api_key: str,
    model: str,
    base_url: str = OPENROUTER_BASE_URL,
    timeout: int = 720,
    max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Send a chat completions request and return the full parsed JSON response.

    Unlike ``call_openrouter`` (which returns only content/usage/agent_id), this
    returns the raw response dict so the session can inspect ``tool_calls`` and
    ``finish_reason``.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body: dict[str, Any] = {"model": model, "messages": messages}
    if max_tokens is not None and max_tokens > 0:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Evan-Kim2028/agent-fleet",
        "X-Title": "agent-fleet",
    }
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenRouter transport error: {exc.reason}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenRouter returned non-JSON body: {raw[:200]}") from exc


def call_openrouter(
    prompt: str,
    *,
    api_key: str,
    model: str,
    base_url: str = OPENROUTER_BASE_URL,
    timeout: int = 720,
    max_tokens: int | None = None,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Call OpenRouter chat completions (stateless). Returns ``(content, usage, agent_id)``.

    ``usage`` is the raw OpenRouter usage dict (includes nested
    ``prompt_tokens_details.cached_tokens``). Callers normalize via
    ``_normalize_openrouter_usage``.
    ``agent_id`` is OpenRouter's response ``id`` (e.g. ``gen-...``) when present.
    Raises ``RuntimeError`` on non-2xx responses or transport errors so the
    backend's ``run()`` can fold them into an error ``LLMResult``.
    """
    data = _call_openrouter_raw(
        [{"role": "user", "content": prompt}],
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout=timeout,
        max_tokens=max_tokens,
    )
    choices = data.get("choices") or []
    content = ""
    if choices:
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        # Reasoning models (e.g. tencent/hy3:free) may put output in `reasoning`
        # when cut off by max_tokens before producing a `content` field.
        if not content and message.get("reasoning"):
            finish = choices[0].get("finish_reason") or ""
            raise RuntimeError(
                f"OpenRouter returned reasoning but no content (finish_reason={finish!r}). "
                f"Increase max_tokens — the model ran out before producing output."
            )
    usage_raw = data.get("usage")
    # Return the raw usage dict (including nested prompt_tokens_details.cached_tokens)
    # so _normalize_openrouter_usage can extract cache hits. The caller normalizes.
    usage: dict[str, Any] | None = usage_raw if isinstance(usage_raw, dict) else None
    agent_id = data.get("id")
    return content, usage, (str(agent_id) if agent_id else None)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenRouterLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None
    usage: dict[str, int] | None = None
    mcp_tool_calls: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Text-mode tool call fallback parser
# ---------------------------------------------------------------------------

# Matches <tool_call:ID>Name\nparameter: key: value\n</tool_call:ID>
# and <tool_call>Name\nparameter: key: value\n</tool_call>
_TOOL_CALL_XML_RE = re.compile(
    r"<tool_call(?::[a-zA-Z0-9_-]+)?>\s*\n?(.*?)\n?</tool_call(?::[a-zA-Z0-9_-]+)?>",
    re.DOTALL,
)
# Matches <tool_calls:ID>...</tool_calls:ID> wrapper (skip — we parse inner calls)
_TOOL_CALLS_WRAPPER_RE = re.compile(
    r"</?tool_calls(?::[a-zA-Z0-9_-]+)?>",
)
# Matches parameter lines: "parameter: key: value" or "parameter: key=value"
_PARAM_LINE_RE = re.compile(
    r"^parameter:\s*(\w+):\s*(.*)$|^parameter:\s*(\w+)\s*=\s*(.*)$",
)
# Matches JSON code blocks containing a tool call
_JSON_TOOL_CALL_RE = re.compile(
    r"```(?:json)?\s*\n(\{[^`]*?\})\s*\n```",
    re.DOTALL,
)

_KNOWN_TOOLS = {"read_file", "write_file", "run_command", "list_files"}

# Some models use Cursor/Claude-Code-style tool names instead of our defined
# names. Map common aliases to our canonical tool names.
_TOOL_ALIASES = {
    "read": "read_file",
    "readfile": "read_file",
    "cat": "read_file",
    "write": "write_file",
    "writefile": "write_file",
    "edit": "write_file",
    "bash": "run_command",
    "shell": "run_command",
    "cmd": "run_command",
    "command": "run_command",
    "run": "run_command",
    "ls": "list_files",
    "list": "list_files",
    "listfiles": "list_files",
    "list_dir": "list_files",
    "lsdir": "list_files",
}


def _canonical_tool_name(name: str) -> str | None:
    """Resolve a tool name (case-insensitive) to a canonical name, or None."""
    lower = name.lower().strip()
    if lower in _KNOWN_TOOLS:
        return lower
    return _TOOL_ALIASES.get(lower)


def _parse_text_tool_calls(content: str) -> list[tuple[str, dict[str, Any]]] | None:
    """Detect and parse tool calls emitted as text in the content field.

    Some models (notably tencent/hy3:free under complex prompts) emit tool
    calls as pseudo-XML or JSON text in the ``content`` field instead of using
    the structured ``tool_calls`` response field. This parser detects common
    text-mode formats and converts them to ``(name, args)`` tuples so the
    session loop can execute them.

    Returns ``None`` if no text-mode tool calls were found (the content is a
    genuine final answer). Returns a list (possibly empty) if text-mode tool
    calls were detected.
    """
    if not content:
        return None

    found: list[tuple[str, dict[str, Any]]] = []

    # Format 1: Pseudo-XML — <tool_call:ID>Name\nparameter: key: value\n</tool_call:ID>
    # Strip the wrapper tags first, then parse inner <tool_call> blocks.
    stripped = _TOOL_CALLS_WRAPPER_RE.sub("", content)
    for match in _TOOL_CALL_XML_RE.finditer(stripped):
        body = match.group(1).strip()
        lines = body.split("\n")
        if not lines:
            continue
        canonical = _canonical_tool_name(lines[0].strip())
        if canonical is None:
            continue
        args: dict[str, Any] = {}
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            pm = _PARAM_LINE_RE.match(line)
            if pm:
                key = pm.group(1) or pm.group(3)
                val = (pm.group(2) or pm.group(4) or "").strip()
                args[key] = val
        found.append((canonical, _normalize_args(canonical, args)))

    if found:
        return found

    # Format 2: JSON code blocks — ```json\n{"name": "read_file", "arguments": {...}}\n```
    for match in _JSON_TOOL_CALL_RE.finditer(content):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        raw_name = obj.get("name") or obj.get("function") or obj.get("tool")
        canonical = _canonical_tool_name(str(raw_name)) if raw_name else None
        if canonical:
            args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            found.append(
                (canonical, _normalize_args(canonical, args if isinstance(args, dict) else {}))
            )

    return found if found else None


# Parameter name aliases — models use different param names than our schema.
_PARAM_ALIASES = {
    "file_path": "path",
    "file": "path",
    "filename": "path",
    "filepath": "path",
    "cmd": "command",
    "cmd_str": "command",
    "shell_command": "command",
    "dir": "path",
    "directory": "path",
    "folder": "path",
}


def _normalize_args(_tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Normalize parameter names to our canonical schema."""
    normalized: dict[str, Any] = {}
    for key, val in args.items():
        canonical_key = _PARAM_ALIASES.get(key.lower(), key)
        normalized[canonical_key] = val
    return normalized


# Matches fabricated tool responses and think blocks the model generates
# in text mode: <tool_response:ID>...</tool_response:ID>, <think:ID>...</think:ID>,
# <tool_responses:ID>...</tool_responses:ID>
_FABRICATED_BLOCK_RE = re.compile(
    r"</?(?:tool_response|tool_responses|think|thinking)(?::[a-zA-Z0-9_-]+)?>",
)


def _strip_fabricated_responses(content: str) -> str:
    """Remove fabricated tool response/think tags from text-mode content.

    Models in text mode sometimes generate both the tool call AND a fabricated
    response in the same content block. Strip the fabricated response tags so
    only the tool call remains (we execute it and feed back the real result).
    """
    return _FABRICATED_BLOCK_RE.sub("", content)


# ---------------------------------------------------------------------------
# Agentic session with tool-use loop
# ---------------------------------------------------------------------------


class OpenRouterSession:
    """Durable session that drives a tool-calling loop against OpenRouter.

    Maintains conversation history across ``send()`` calls so the model retains
    context across plan → research → synthesize → implement → verify → review.
    Each ``send()`` appends the user prompt to the history, loops on tool_calls
    until the model produces a final text response, then returns the result.
    """

    def __init__(
        self,
        *,
        backend: OpenRouterBackend,
        cwd: Path,
        model: str,
        persona_name: str,
    ) -> None:
        self._backend = backend
        self._cwd = cwd
        self._model = model
        self._persona_name = persona_name
        self._messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    f"You are a {persona_name} agent working in a git workspace at {cwd}. "
                    "You have access to tools: read_file, write_file, run_command, list_files. "
                    "You MUST call tools using the structured tool_calls mechanism — "
                    "do NOT write tool calls as text in your response. "
                    "Always verify your changes by reading the file back after writing. "
                    "When the task is complete, respond with a summary of what you did."
                ),
            }
        ]
        self._agent_id: str | None = None

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
        expect_mcp_tools: bool = False,  # noqa: ARG002
        mcp_requirement: McpRequirement | None = None,
    ) -> OpenRouterLLMResult:
        del mcp_requirement
        scope_prefixes = [
            tool.removeprefix("path:") for tool in (allowed_tools or []) if tool.startswith("path:")
        ]
        scope_note = ""
        if scope_prefixes:
            scope_note = (
                "\n\nHard scope constraint: only modify files under these prefixes: "
                + ", ".join(scope_prefixes)
            )

        self._messages.append({"role": "user", "content": f"{prompt}{scope_note}"})
        t0 = time.monotonic()
        tool_calls_made: list[str] = []
        total_usage: dict[str, int] = {}

        try:
            for _iteration in range(_MAX_TOOL_ITERATIONS):
                data = _call_openrouter_raw(
                    self._messages,
                    api_key=self._backend.api_key,
                    model=self._model,
                    base_url=self._backend.base_url,
                    timeout=timeout_s if timeout_s > 0 else 720,
                    max_tokens=max_tokens,
                    tools=_FILE_TOOLS,
                )
                if not self._agent_id:
                    raw_id = data.get("id")
                    if raw_id:
                        self._agent_id = str(raw_id)

                # Accumulate usage across all turns in this send()
                turn_usage = _normalize_openrouter_usage(data.get("usage"))
                if turn_usage:
                    for k, v in turn_usage.items():
                        total_usage[k] = total_usage.get(k, 0) + v

                choices = data.get("choices") or []
                if not choices:
                    return OpenRouterLLMResult(
                        stdout="",
                        stderr="OpenRouter returned no choices",
                        exit_code=1,
                        duration_s=time.monotonic() - t0,
                        agent_id=self._agent_id,
                        usage=total_usage or None,
                        mcp_tool_calls=tuple(tool_calls_made),
                    )

                choice = choices[0]
                message = choice.get("message") or {}
                finish_reason = choice.get("finish_reason") or ""

                # If the model produced tool_calls, execute them and continue.
                tool_calls = message.get("tool_calls")
                if tool_calls and finish_reason == "tool_calls":
                    # Append the assistant message (with tool_calls) to history.
                    self._messages.append(
                        {
                            "role": "assistant",
                            "content": message.get("content"),
                            "tool_calls": tool_calls,
                        }
                    )
                    for tc in tool_calls:
                        func = tc.get("function") or {}
                        tool_name = func.get("name", "")
                        try:
                            tool_args = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            tool_args = {}
                        tool_calls_made.append(tool_name)
                        result = _execute_tool(
                            tool_name,
                            tool_args,
                            cwd=self._cwd,
                            scope_prefixes=scope_prefixes,
                        )
                        self._messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.get("id", ""),
                                "content": result,
                            }
                        )
                    continue

                # finish_reason == "stop" (or anything else) → we're done.
                content = str(message.get("content") or "")
                # Handle reasoning models that ran out of tokens mid-reasoning.
                if not content and message.get("reasoning"):
                    content = (
                        f"[Model produced reasoning but no content "
                        f"(finish_reason={finish_reason!r}). "
                        f"Increase max_tokens.]"
                    )
                    self._messages.append({"role": "assistant", "content": content})
                    return OpenRouterLLMResult(
                        stdout="",
                        stderr=content,
                        exit_code=1,
                        duration_s=time.monotonic() - t0,
                        agent_id=self._agent_id,
                        usage=total_usage or None,
                        mcp_tool_calls=tuple(tool_calls_made),
                    )

                # Text-mode tool call fallback: some models emit tool calls as
                # text in the content field instead of using the structured
                # tool_calls response field. Detect and execute them, then
                # continue the loop instead of treating this as a final answer.
                text_tool_calls = _parse_text_tool_calls(content)
                if text_tool_calls is not None:
                    # Strip fabricated tool responses/think blocks so the model
                    # doesn't confuse its own imagined responses with real results.
                    clean_content = _strip_fabricated_responses(content)
                    self._messages.append({"role": "assistant", "content": clean_content})
                    for idx, (tool_name, tool_args) in enumerate(text_tool_calls):
                        tool_calls_made.append(tool_name)
                        result = _execute_tool(
                            tool_name,
                            tool_args,
                            cwd=self._cwd,
                            scope_prefixes=scope_prefixes,
                        )
                        self._messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": f"text-call-{idx}",
                                "content": result,
                            }
                        )
                    continue

                self._messages.append({"role": "assistant", "content": content})
                ctx = get_run_context()
                _log_llm_usage(
                    phase=ctx.phase if ctx is not None else None,
                    model=self._model,
                    usage=total_usage or None,
                    duration_s=time.monotonic() - t0,
                    agent_id=self._agent_id,
                )
                return OpenRouterLLMResult(
                    stdout=content,
                    stderr="",
                    exit_code=0,
                    duration_s=time.monotonic() - t0,
                    agent_id=self._agent_id,
                    usage=total_usage or None,
                    mcp_tool_calls=tuple(tool_calls_made),
                )

            # Hit the iteration cap.
            return OpenRouterLLMResult(
                stdout="",
                stderr=f"OpenRouter session hit max tool iterations ({_MAX_TOOL_ITERATIONS})",
                exit_code=1,
                duration_s=time.monotonic() - t0,
                agent_id=self._agent_id,
                usage=total_usage or None,
                mcp_tool_calls=tuple(tool_calls_made),
            )

        except Exception as exc:
            return OpenRouterLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
                agent_id=self._agent_id,
                usage=total_usage or None,
                mcp_tool_calls=tuple(tool_calls_made),
            )

    def dispose(self) -> None:
        """No persistent connections to close — no-op."""


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class _OpenRouterErrorSession:
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
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> OpenRouterLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools, expect_mcp_tools, mcp_requirement
        return OpenRouterLLMResult(stdout="", stderr=self._message, exit_code=1, duration_s=0.0)

    def dispose(self) -> None:
        pass


class OpenRouterBackend:
    """Run prompts through OpenRouter's OpenAI-compatible chat completions API."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        base_url: str = OPENROUTER_BASE_URL,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url

    def create_session(
        self,
        *,
        persona_name: str,
        cwd: Path,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,  # noqa: ARG002
        model: str | None = None,
        mode: AgentMode | str | None = None,  # noqa: ARG002
    ) -> OpenRouterSession | _OpenRouterErrorSession:
        """Create a durable tool-calling session.

        OpenRouter doesn't support MCP servers natively — the session uses
        built-in file/shell tools instead. The ``mcp_servers`` and ``mode``
        parameters are accepted for protocol compatibility but ignored.
        """
        if not self.api_key:
            return _OpenRouterErrorSession("OPENROUTER_API_KEY is not set")
        return OpenRouterSession(
            backend=self,
            cwd=cwd,
            model=model or self.model,
            persona_name=persona_name,
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
    ) -> OpenRouterLLMResult:
        del memory_limit, mode, cwd

        if not self.api_key:
            return OpenRouterLLMResult(
                stdout="",
                stderr="OPENROUTER_API_KEY is not set",
                exit_code=1,
                duration_s=0.0,
            )

        selected_model = model or self.model
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
            content, usage, agent_id = call_openrouter(
                prompt_with_scope,
                api_key=self.api_key,
                model=selected_model,
                base_url=self.base_url,
                timeout=timeout_s if timeout_s > 0 else 720,
                max_tokens=max_tokens,
            )
            duration_s = time.monotonic() - t0
            # Wire usage into the fleet's observability system.
            normalized = _normalize_openrouter_usage(usage)
            if normalized:
                ctx = get_run_context()
                _log_llm_usage(
                    phase=ctx.phase if ctx is not None else None,
                    model=selected_model,
                    usage=normalized,
                    duration_s=duration_s,
                    agent_id=agent_id,
                )
            return OpenRouterLLMResult(
                stdout=content,
                stderr="",
                exit_code=0,
                duration_s=duration_s,
                agent_id=agent_id,
                usage=normalized,
            )
        except Exception as exc:
            return OpenRouterLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
            )
