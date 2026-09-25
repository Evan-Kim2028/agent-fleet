"""Lazy-exit detection — ported from the ``fbrun`` bash driver.

``fbrun`` treated a run as a failure when the model finished *without doing the
work*. Two shapes, both of which otherwise read as success (exit code 0, a
fluent final message):

1. **No tool calls at all.** The model narrated a plan and stopped.
2. **Very few tool calls plus a refusal.** The model tried, concluded it could
   not, and said so politely — e.g. the tools were unavailable in a headless
   run, so it described what it *would* have done.

Both are silent failures: a naive caller sees ``rc == 0`` and moves on, the lane
looks done, and the work never happened. The port keeps fbrun's thresholds and
exit code so the operator's muscle memory ("exit 86 = lazy exit") still holds.

fbrun's rule, verbatim in spirit::

    if rc == 0:
        tool_calls = count of tool_completed events
        if tool_calls == 0:
            rc = 86
        elif tool_calls < 25 and REFUSAL_RE.search(final_text):
            rc = 86
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: fbrun's exit code for a lazy exit. Preserved so operators can still read it.
LAZY_EXIT_CODE = 86

#: Below this many tool calls, a refusal is treated as a lazy exit (fbrun used 25).
REFUSAL_TOOL_CALL_THRESHOLD = 25

#: fbrun's refusal patterns, verbatim. Matched case-insensitively against the
#: final text only when the tool-call count is low.
REFUSAL_PATTERNS: tuple[str, ...] = (
    r"(can.?t|cannot|could ?n.?t|unable to)\s+(execute|complete|run|perform|access)",
    r"tools?\s+(were|was|are|is)\s+(not\s+)?(un)?available",
    r"required\s+(shell|file)\s+tools?",
)

_REFUSAL_RE = re.compile("|".join(REFUSAL_PATTERNS), re.IGNORECASE)

#: Event subtypes that count as a completed tool call in the cmd JSONL stream.
TOOL_EVENT_KEYS = ("tool_completed", "tool_result", "tool_use")

#: Subtypes marking a tool call that *failed*. Feeds the tool-error percentage in
#: ``lanes status``: a lane whose tools fail 80% of the time is wedged on
#: permissions or a wrong path long before it declares itself stalled.
TOOL_ERROR_KEYS = ("error", "tool_error", "failed")


def count_tool_errors(stream_text: str) -> int:
    """Count tool calls that reported an error, using the same tolerance.

    Only counted for lines that are tool events, so a stray top-level ``error``
    (a provider hiccup, a malformed stream line) is not attributed to tool
    activity and cannot inflate the ratio past 100%.
    """
    count = 0
    for line in (stream_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        subtype = str(record.get("subtype") or "")
        etype = str(record.get("type") or "")
        if etype not in TOOL_EVENT_KEYS and subtype not in TOOL_EVENT_KEYS:
            continue
        if any(
            key in (str(record.get("subtype") or ""), str(record.get("type") or ""))
            for key in TOOL_ERROR_KEYS
        ):
            count += 1
    return count


@dataclass(frozen=True)
class LazyExitVerdict:
    """Whether a run was a lazy exit, and why."""

    lazy: bool
    tool_calls: int
    reason: str = ""
    exit_code: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "lazy": self.lazy,
            "tool_calls": self.tool_calls,
            "reason": self.reason,
            "exit_code": self.exit_code,
        }


def looks_like_refusal(text: str) -> bool:
    """True when *text* matches fbrun's refusal patterns."""
    return bool(_REFUSAL_RE.search(text or ""))


def count_tool_calls(stream_text: str) -> int:
    """Count completed tool calls in a cmd JSONL stream.

    Tolerant by design: counts any JSONL line carrying a tool-completion marker,
    ignoring unparseable lines. A partially-flushed stream is normal when a run
    is interrupted, and the count is only ever used as a threshold.
    """
    count = 0
    for line in (stream_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        subtype = str(record.get("subtype") or "")
        etype = str(record.get("type") or "")
        if etype in TOOL_EVENT_KEYS or subtype in TOOL_EVENT_KEYS:
            count += 1
    return count


def extract_final_text(stream_text: str) -> str:
    """Extract the final assistant text from a cmd JSONL stream.

    ``fbrun``'s extractor took the last ``{"type": "result"}`` record's
    ``finalText`` field, falling back to the literal ``"NO RESULT EVENT"`` when
    no such record existed. Same contract, because the caller (and the
    ``cmd_ok`` check in the old ``fblane``) tested for exactly that sentinel.
    """
    final: dict | None = None
    for line in (stream_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            final = record
    if final is None:
        return "NO RESULT EVENT"
    return str(final.get("finalText") or "")


def judge_run(
    *,
    exit_code: int,
    stream_text: str,
    final_text: str | None = None,
    refusal_threshold: int = REFUSAL_TOOL_CALL_THRESHOLD,
) -> LazyExitVerdict:
    """Judge a completed run. A non-zero exit is returned as-is, not re-judged.

    Only a zero exit can be reclassified as a lazy exit — that is the entire
    point, since a real failure already signals itself.
    """
    tool_calls = count_tool_calls(stream_text)
    text = final_text if final_text is not None else extract_final_text(stream_text)

    if exit_code != 0:
        return LazyExitVerdict(lazy=False, tool_calls=tool_calls, exit_code=exit_code)

    if tool_calls == 0:
        return LazyExitVerdict(
            lazy=True,
            tool_calls=0,
            reason="no tool calls: the run finished without doing any work",
            exit_code=LAZY_EXIT_CODE,
        )

    if tool_calls < refusal_threshold and looks_like_refusal(text):
        return LazyExitVerdict(
            lazy=True,
            tool_calls=tool_calls,
            reason=f"refusal after only {tool_calls} tool call(s)",
            exit_code=LAZY_EXIT_CODE,
        )

    return LazyExitVerdict(lazy=False, tool_calls=tool_calls, exit_code=0)


def judge_stream_file(
    path: Path | str, *, exit_code: int, refusal_threshold: int = REFUSAL_TOOL_CALL_THRESHOLD
) -> LazyExitVerdict:
    """Convenience wrapper that reads the JSONL stream from *path*."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return LazyExitVerdict(
            lazy=True, tool_calls=0, reason=f"stream unreadable: {path}", exit_code=LAZY_EXIT_CODE
        )
    return judge_run(exit_code=exit_code, stream_text=text, refusal_threshold=refusal_threshold)
