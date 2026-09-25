"""Structured JSON extraction and schema-checked backend calls for the gate.

The gate talks to four different roles (lens reviewer, verifier, judge, judge
recheck), all of which must answer in JSON validated against a contract schema.
This module owns that one mechanism: prompt the model for a single fenced JSON
block, extract it, validate it, and retry once with the validation error fed
back — the same two-attempt pattern the rest of the fleet uses, with the error
text reused across roles.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.hooks import LLMBackend
    from agent_fleet.slots import SlotPool

logger = logging.getLogger(__name__)

_FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_RETRY_NOTE = (
    "Your previous response could not be parsed or failed schema validation. "
    "Respond again with ONLY the JSON object — no prose, no markdown fences "
    "beyond the one json block, no commentary. It MUST start with '{' and end "
    "with '}'."
)


class StructuredCallError(RuntimeError):
    """Raised when a model answer cannot be parsed or validated after retries."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced JSON object from *text*.

    Walks brace depth with string/escape awareness so a ``}`` inside a string
    does not end the object early; falls back to the last fenced ```json block
    (the reference gate read answers in reverse order, since the final block is
    the model corrected itself).
    """
    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start=start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start : i + 1])
    blocks = _FENCED_RE.findall(text)
    for block in reversed(blocks):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no balanced JSON object found in model output")


@dataclass(frozen=True)
class StructuredAnswer:
    """A validated structured answer plus the raw text it came from."""

    data: dict[str, Any]
    raw: str


def call_structured(
    backend: LLMBackend,
    prompt: str,
    *,
    model: str,
    cwd: Path,
    timeout_s: int,
    validate: Any,  # noqa: ANN401 - a contracts.validate_* callable
    mode: AgentMode = "agent",
    slot: SlotPool | None = None,
    slot_timeout_s: float | None = None,
    max_attempts: int = 2,
) -> StructuredAnswer:
    """Prompt *backend* for JSON, extract it, and validate it.

    On a parse or validation failure the error is appended to the prompt and the
    call is retried once; a second failure raises :class:`StructuredCallError`.
    *slot* is the machine-wide concurrency pool the call holds while running.
    """
    from contextlib import nullcontext

    attempt_prompt = prompt
    last_error = ""
    raw = ""
    for attempt in range(max_attempts):
        guard = slot.slot(timeout_s=slot_timeout_s) if slot is not None else nullcontext()
        with guard:
            result = backend.run(
                attempt_prompt,
                max_tokens=0,
                timeout_s=timeout_s,
                cwd=cwd,
                model=model,
                mode=mode,
            )
        if result.exit_code != 0 or not (result.stdout or "").strip():
            last_error = f"backend call failed (exit {result.exit_code}): {result.stderr[:300]}"
        else:
            raw = result.stdout
            try:
                data = extract_json_object(raw)
                validate(data)
            except Exception as exc:
                last_error = str(exc)[:400]
                logger.debug("gate structured call %d invalid: %s", attempt, last_error)
            else:
                return StructuredAnswer(data=data, raw=raw)
        # Feed the failure back so the retry corrects rather than repeats.
        attempt_prompt = f"{prompt}\n\n{_RETRY_NOTE}\n\nPrevious failure: {last_error}"
    raise StructuredCallError(
        f"structured call failed after {max_attempts} attempts: {last_error}; "
        f"raw output: {raw[:300]}"
    )
