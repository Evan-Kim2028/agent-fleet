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
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.hooks import LLMBackend
    from agent_fleet.slots import SlotPool

logger = logging.getLogger(__name__)

#: The cmd backend's exit code for "stopped at --max-turns", partial or not.
#: A run that hit it never reached a verdict, so the gate must not read its
#: output as one.
TURN_CAP_EXIT = 8

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
        "nothing new; no prose outside the block. Do not run any tools.\n"
        "Report the findings you have ALREADY established. If your investigation "
        "was cut short before you reached a conclusion, report what you did "
        "establish rather than an empty list: an empty findings list asserts the "
        "change is clean, and you have not shown that.\n\n"
        f"Validation error: {error[:300]}\n\n"
        "===== REQUIRED FORMAT (from the original instructions) =====\n"
        f"{original[-4000:]}\n\n"
        "===== YOUR PREVIOUS ANSWER =====\n"
        f"{raw}"
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

    ``raw`` and ``duration_s`` carry what the agent actually said so the caller
    can persist a failed call: a lost finding is only diagnosable from the text
    that contained it.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "invalid",
        raw: str = "",
        duration_s: float = 0.0,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.raw = raw
        self.duration_s = duration_s
        self.exit_code = exit_code


def _balanced_spans(text: str, open_ch: str, close_ch: str) -> list[tuple[int, int]]:
    """Top-level ``open``..``close`` spans, ignoring delimiters inside strings."""
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
        if ch == open_ch:
            if depth == 0:
                start = i
            depth += 1
        elif ch == close_ch and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append((start, i + 1))
    return spans


def json_candidates(text: str, *, list_key: str | None = None) -> list[dict[str, Any]]:
    """Every JSON object in *text*, most-likely-final first.

    Order is **position in the text**, last to first, across both fenced
    ```` ```json ```` blocks and bare balanced ``{...}`` objects. Position is the
    only honest signal about which answer is final: a reviewer that echoes the
    schema template in a fence and then answers unfenced has put its template
    first and its findings last, and ranking by *kind* returned the template
    instead (the 2026-09-25 pilot on lake #3541, where every lens lost its
    findings this way and the gate reported ``candidates=0``).

    Prose containing braces (code snippets, dict literals) no longer blocks
    extraction: unparseable candidates are skipped.

    With *list_key*, a bare top-level ``[...]`` array is also a candidate,
    wrapped as ``{list_key: [...]}``. A lens that answers the findings list
    directly instead of nesting it parsed to nothing at all, which reads exactly
    like a clean review.
    """
    spans: list[tuple[int, int, str]] = [
        (lo, hi, "{}") for lo, hi in _balanced_spans(text, "{", "}")
    ]
    if list_key:
        spans.extend((lo, hi, "[]") for lo, hi in _balanced_spans(text, "[", "]"))

    out: list[dict[str, Any]] = []
    for lo, hi, kind in sorted(spans, key=lambda s: s[0], reverse=True):
        try:
            parsed = json.loads(text[lo:hi])
        except json.JSONDecodeError:
            continue
        candidate: Any = parsed
        if kind == "[]" and list_key is not None:
            if not isinstance(parsed, list):
                continue
            candidate = {list_key: parsed}
        if isinstance(candidate, dict) and candidate not in out:
            out.append(candidate)
    return out


def extract_json_object(text: str, *, list_key: str | None = None) -> dict[str, Any]:
    """The most-likely-final JSON object in *text* (see :func:`json_candidates`)."""
    found = json_candidates(text, list_key=list_key)
    if not found:
        raise ValueError("no balanced JSON object found in model output")
    return found[0]


def _first_valid(
    text: str,
    validate: Any,  # noqa: ANN401
    *,
    list_key: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Last candidate that passes *validate*, or ``(None, last_error)``.

    Scans final-first and returns the *most recent* valid candidate, so an
    earlier template that happens to validate (an empty ``{"findings": []}``
    does) can never mask the real answer behind it.
    """
    cands = json_candidates(text, list_key=list_key)
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
    duration_s: float = 0.0


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
    list_key: str | None = None,
) -> StructuredAnswer:
    """Prompt *backend* for JSON, extract it, and validate it.

    On a parse or validation failure the error is appended to the prompt and the
    call is retried once; a second failure raises :class:`StructuredCallError`.
    *slot* is the machine-wide concurrency pool the call holds while running.
    *list_key* also accepts a bare top-level array as ``{list_key: [...]}``.
    """
    from contextlib import nullcontext

    prompt = f"{prompt}\n\n{_FINAL_FORMAT_NOTE}"
    attempt_prompt = prompt
    last_error = ""
    last_kind = "invalid"
    raw = ""
    exit_code = 1
    started = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - started

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
            # Exit 8 is the backend's turn cap: the run stopped mid-review
            # without reaching a verdict. Treating it as a completed answer is
            # what let a lens spend its whole turn budget investigating, never
            # write its JSON, and report `candidates=0` — indistinguishable from
            # a clean review. It is dead evidence, so it fails like a crash.
            if result.exit_code == TURN_CAP_EXIT:
                last_error = (
                    f"turn cap hit (exit {TURN_CAP_EXIT}) before the agent produced a "
                    f"final answer; last output was {len(result.stdout or '')} chars"
                )
            else:
                last_error = f"backend call failed (exit {result.exit_code}): {result.stderr[:300]}"
            last_kind = "dead"
            raw = result.stdout or ""
            exit_code = result.exit_code
        else:
            raw = result.stdout
            exit_code = 0
            data, last_error = _first_valid(raw, validate, list_key=list_key)
            if data is not None:
                return StructuredAnswer(data=data, raw=raw, duration_s=elapsed())
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
                data, repair_error = _first_valid(fixed.stdout, validate, list_key=list_key)
                if data is not None:
                    logger.debug("gate structured call %d repaired", attempt)
                    return StructuredAnswer(data=data, raw=fixed.stdout, duration_s=elapsed())
                last_error = f"{last_error}; repair: {repair_error}"
        # Feed the failure back so the retry corrects rather than repeats.
        attempt_prompt = f"{prompt}\n\n{_RETRY_NOTE}\n\nPrevious failure: {last_error}"
    raise StructuredCallError(
        f"structured call failed after {max_attempts} attempts: {last_error}; "
        f"raw output: {raw[:300]}",
        kind=last_kind,
        raw=raw,
        duration_s=elapsed(),
        exit_code=exit_code,
    )
