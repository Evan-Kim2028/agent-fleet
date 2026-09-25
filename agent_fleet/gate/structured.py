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
_FINAL_FORMAT_NOTE = (
    "FINAL ANSWER FORMAT (repeated on purpose): your final message must be exactly "
    "one ```json fenced block matching the schema/example given above. No prose "
    "after it."
)


def _repair_prompt(original: str, raw: str, error: str) -> str:
    return (
        "Your previous answer below contains the right analysis but not in the "
        "required format. Rewrite it as exactly one ```json fenced block that "
        "conforms to the required schema. Keep every finding/verdict you made; add "
        "nothing new; no prose outside the block. Do not run any tools.\n\n"
        f"Validation error: {error[:300]}\n\n"
        "===== REQUIRED FORMAT (from the original instructions) =====\n"
        f"{original[-4000:]}\n\n"
        "===== YOUR PREVIOUS ANSWER =====\n"
        f"{raw[-12000:]}"
    )


_RETRY_NOTE = (
    "Your previous response could not be parsed or failed schema validation. "
    "Respond again with ONLY the JSON object — no prose, no markdown fences "
    "beyond the one json block, no commentary. It MUST start with '{' and end "
    "with '}'."
)


class StructuredCallError(RuntimeError):
    """Raised when a model answer cannot be parsed or validated after retries.

    ``kind`` tells the gate what the failure means for the verdict:
    ``"dead"`` — the agent exited non-zero or produced no output (killed,
    crashed, timed out): there is NO evidence either way; ``"invalid"`` — the
    agent answered, but not in the required shape.
    """

    def __init__(self, message: str, *, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


def json_candidates(text: str) -> list[dict[str, Any]]:
    """Every JSON object in *text*, most-likely-final first.

    Order: fenced ```json blocks from last to first (a model that corrected
    itself puts the final answer last), then every balanced top-level ``{...}``
    from last to first. Prose containing braces (code snippets, dict literals)
    no longer blocks extraction: unparseable candidates are skipped.
    """
    out: list[dict[str, Any]] = []
    for block in reversed(_FENCED_RE.findall(text)):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    spans: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"' and depth > 0:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append((start, i + 1))
    for lo, hi in reversed(spans):
        try:
            parsed = json.loads(text[lo:hi])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed not in out:
            out.append(parsed)
    return out


def extract_json_object(text: str) -> dict[str, Any]:
    """The most-likely-final JSON object in *text* (see :func:`json_candidates`)."""
    found = json_candidates(text)
    if not found:
        raise ValueError("no balanced JSON object found in model output")
    return found[0]


def _first_valid(text: str, validate: Any) -> tuple[dict[str, Any] | None, str]:  # noqa: ANN401
    """First candidate that passes *validate*, or ``(None, last_error)``."""
    cands = json_candidates(text)
    if not cands:
        return None, "no balanced JSON object found in model output"
    last = ""
    for cand in cands:
        try:
            validate(cand)
        except Exception as exc:
            last = str(exc)[:400]
            continue
        return cand, ""
    return None, last


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

    prompt = f"{prompt}\n\n{_FINAL_FORMAT_NOTE}"
    attempt_prompt = prompt
    last_error = ""
    last_kind = "invalid"
    raw = ""

    def guard_factory() -> Any:  # noqa: ANN401
        return slot.slot(timeout_s=slot_timeout_s) if slot is not None else nullcontext()

    for attempt in range(max_attempts):
        with guard_factory():
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
            last_kind = "dead"
        else:
            raw = result.stdout
            data, last_error = _first_valid(raw, validate)
            if data is not None:
                return StructuredAnswer(data=data, raw=raw)
            last_kind = "invalid"
            logger.debug("gate structured call %d invalid: %s", attempt, last_error)
            # REPAIR turn: the agent did the work but answered in the wrong shape.
            # Hand it its own answer and ask only for the reformat — cheap, and it
            # keeps the analysis instead of redoing it from scratch.
            repair = _repair_prompt(prompt, raw, last_error)
            with guard_factory():
                fixed = backend.run(
                    repair,
                    max_tokens=0,
                    timeout_s=min(timeout_s, 600),
                    cwd=cwd,
                    model=model,
                    mode="plan",
                )
            if fixed.exit_code == 0 and (fixed.stdout or "").strip():
                data, repair_error = _first_valid(fixed.stdout, validate)
                if data is not None:
                    logger.debug("gate structured call %d repaired", attempt)
                    return StructuredAnswer(data=data, raw=fixed.stdout)
                last_error = f"{last_error}; repair: {repair_error}"
        # Feed the failure back so the retry corrects rather than repeats.
        attempt_prompt = f"{prompt}\n\n{_RETRY_NOTE}\n\nPrevious failure: {last_error}"
    raise StructuredCallError(
        f"structured call failed after {max_attempts} attempts: {last_error}; "
        f"raw output: {raw[:300]}",
        kind=last_kind,
    )
