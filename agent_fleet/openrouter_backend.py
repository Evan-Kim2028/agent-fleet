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
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "tencent/hy3:free"


def call_openrouter(
    prompt: str,
    *,
    api_key: str,
    model: str,
    base_url: str = OPENROUTER_BASE_URL,
    timeout: int = 720,
    max_tokens: int | None = None,
) -> tuple[str, dict[str, int] | None, str | None]:
    """Call OpenRouter chat completions. Returns ``(content, usage, agent_id)``.

    ``agent_id`` is OpenRouter's response ``id`` (e.g. ``gen-...``) when present.
    Raises ``RuntimeError`` on non-2xx responses or transport errors so the
    backend's ``run()`` can fold them into an error ``LLMResult``.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None and max_tokens > 0:
        body["max_tokens"] = max_tokens

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter recommends a referrer + title for app attribution.
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
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenRouter returned non-JSON body: {raw[:200]}") from exc

    choices = data.get("choices") or []
    content = ""
    if choices:
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        # Reasoning models (e.g. tencent/hy3:free) may put output in `reasoning`
        # when cut off by max_tokens before producing a `content` field. If
        # content is empty but reasoning exists, surface the reasoning as
        # stderr diagnostic so the caller knows the model was cut off, and keep
        # stdout empty (the actual answer wasn't produced).
        if not content and message.get("reasoning"):
            finish = choices[0].get("finish_reason") or ""
            raise RuntimeError(
                f"OpenRouter returned reasoning but no content (finish_reason={finish!r}). "
                f"Increase max_tokens — the model ran out before producing output."
            )
    usage_raw = data.get("usage")
    usage: dict[str, int] | None = None
    if isinstance(usage_raw, dict):
        usage = {
            k: int(v)
            for k, v in usage_raw.items()
            if isinstance(v, int | float) and not isinstance(v, bool)
        } or None
    agent_id = data.get("id")
    return content, usage, (str(agent_id) if agent_id else None)


@dataclass(frozen=True)
class OpenRouterLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None
    usage: dict[str, int] | None = None


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
            return OpenRouterLLMResult(
                stdout=content,
                stderr="",
                exit_code=0,
                duration_s=time.monotonic() - t0,
                agent_id=agent_id,
                usage=usage,
            )
        except Exception as exc:
            return OpenRouterLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
            )
