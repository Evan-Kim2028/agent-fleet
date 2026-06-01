"""mine.py — DETERMINISTIC failure-signature mining.

Parses agent fleet run-log NDJSON over the last N days, buckets failures by
signature ``(persona, phase, error_class)``, and ranks them by
``frequency * cost``.  No LLM calls; pure functions; fully unit-testable.

Run-log shape assumed (NDJSON, one JSON object per line)::

    {
        "ts":         "2026-05-18T10:00:00Z",   # ISO-8601 UTC
        "run_id":     "abc123",
        "issue":      42,
        "persona":    "backend",
        "event":      "phase_end",               # phase_start | phase_end | run_start | run_end
        "phase":      "verify",
        "status":     "failed",                  # complete | failed | skipped | started
        "duration_s": 87,                        # optional int/float
        "detail":     "schema_validation_failed: …"   # optional string
    }

The miner processes only ``event == "phase_end"`` records with
``status == "failed"`` (plus ``run_end`` records to extract run-level
failures).  Malformed lines and unknown fields are silently skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Sequence


# ---------------------------------------------------------------------------
# Error-class taxonomy
# ---------------------------------------------------------------------------

class ErrorClass(str, Enum):
    """Normalised error category derived from the ``detail`` field."""

    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    VERIFY_REJECTED = "verify_rejected"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    TIMEOUT = "timeout"
    TOOL_ERROR = "tool_error"
    GIT_COMMIT_FAILED = "git_commit_failed"
    ZERO_DIFF = "zero_diff"
    OTHER = "other"


# Pattern → ErrorClass, applied in order (first match wins).
_ERROR_PATTERNS: list[tuple[re.Pattern[str], ErrorClass]] = [
    (re.compile(r"schema.?validation.?fail", re.IGNORECASE), ErrorClass.SCHEMA_VALIDATION_FAILED),
    (re.compile(r"json.?schema|schema.?error|validation.?error", re.IGNORECASE), ErrorClass.SCHEMA_VALIDATION_FAILED),
    (re.compile(r"verify.?reject|reject.*verify|verification.?fail", re.IGNORECASE), ErrorClass.VERIFY_REJECTED),
    (re.compile(r"review.?change|changes.?request|request.*change", re.IGNORECASE), ErrorClass.REVIEW_CHANGES_REQUESTED),
    (re.compile(r"timeout|timed.?out|deadline.?exceed", re.IGNORECASE), ErrorClass.TIMEOUT),
    (re.compile(r"tool.?error|tool.?fail|tool.?exception", re.IGNORECASE), ErrorClass.TOOL_ERROR),
    (re.compile(r"git.?commit.?fail|commit.?error|nothing.?to.?commit", re.IGNORECASE), ErrorClass.GIT_COMMIT_FAILED),
    (re.compile(r"zero.?diff|empty.?diff|no.?change|diff.?empty", re.IGNORECASE), ErrorClass.ZERO_DIFF),
]


def classify_error(detail: str | None) -> ErrorClass:
    """Derive an :class:`ErrorClass` from a ``detail`` string.

    Returns :attr:`ErrorClass.OTHER` when *detail* is absent or matches
    no pattern.
    """
    if not detail:
        return ErrorClass.OTHER
    for pattern, cls in _ERROR_PATTERNS:
        if pattern.search(detail):
            return cls
    return ErrorClass.OTHER


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureSignature:
    """Canonical failure signature — the grouping key for mining."""

    persona: str
    phase: str
    error_class: ErrorClass


@dataclass(frozen=True)
class TraceRecord:
    """A single raw log record that contributed to a :class:`FailureSignature`."""

    ts: str
    run_id: str | None
    issue: int | None
    persona: str
    phase: str
    detail: str | None
    duration_s: float | None


@dataclass
class SignatureBucket:
    """Aggregated data for one :class:`FailureSignature`."""

    signature: FailureSignature
    count: int = 0
    total_cost: float = 0.0  # sum of duration_s (or 1.0 per record when absent)
    traces: list[TraceRecord] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Ranking score: ``frequency * total_cost``."""
        return float(self.count) * self.total_cost

    def add(self, record: TraceRecord) -> None:
        cost = record.duration_s if record.duration_s is not None else 1.0
        self.count += 1
        self.total_cost += cost
        self.traces.append(record)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp, returning None on any error."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _iter_log_lines(path: Path) -> Iterator[dict]:
    """Yield parsed JSON objects from an NDJSON file; skip malformed lines."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        yield obj


def _iter_records_from_path(
    log_path: Path,
    *,
    days: int,
    cutoff: datetime,
) -> Iterator[dict]:
    """Yield log records from *log_path*.

    Supports two layouts:

    1. **Single flat file** (``data/state/run_log.jsonl`` style) — iterates
       all lines and filters by ``ts >= cutoff``.
    2. **Date-partitioned directory** (``data/events/agent_runs/YYYY-MM-DD.ndjson``
       style) — reads only the files for the relevant date range.
    """
    if log_path.is_dir():
        today = cutoff.date()
        for offset in range(days + 1):
            d = today + timedelta(days=offset)
            # The directory may contain future dates relative to cutoff; we
            # just scan all files in the window regardless of which way the
            # clock has moved since the cutoff was computed.
            for fname in (f"{d.isoformat()}.ndjson", f"{d.isoformat()}.jsonl"):
                fp = log_path / fname
                if fp.exists():
                    yield from _iter_log_lines(fp)
        # Also scan backward from cutoff
        for offset in range(days + 1):
            d = cutoff.date() - timedelta(days=offset)
            for fname in (f"{d.isoformat()}.ndjson", f"{d.isoformat()}.jsonl"):
                fp = log_path / fname
                if fp.exists():
                    yield from _iter_log_lines(fp)
    else:
        # Single flat file — tolerate missing file gracefully.
        if not log_path.exists():
            return
        yield from _iter_log_lines(log_path)


def _is_failure_record(record: dict) -> bool:
    """Return True if this record represents a phase or run failure."""
    event = record.get("event")
    status = record.get("status")
    if event in ("phase_end", "run_end") and status == "failed":
        return True
    # Also capture run_end with non-success statuses that aren't "complete"
    if event == "run_end" and status not in ("complete", "opened_pr", "failed", None):
        # Statuses like "ci_failed", "merge_blocked" etc. count as failures
        if status not in ("complete", "opened_pr", "skipped"):
            return True
    return False


def _record_to_trace(record: dict) -> TraceRecord | None:
    """Convert a raw log dict to a :class:`TraceRecord`, or None if incomplete."""
    persona = record.get("persona")
    phase = record.get("phase")
    if not persona or not isinstance(persona, str):
        return None
    if not phase or not isinstance(phase, str):
        return None
    duration = record.get("duration_s")
    if duration is not None:
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None
    return TraceRecord(
        ts=str(record.get("ts") or ""),
        run_id=str(record.get("run_id")) if record.get("run_id") is not None else None,
        issue=int(record["issue"]) if isinstance(record.get("issue"), (int, float)) else None,
        persona=persona,
        phase=phase,
        detail=str(record["detail"]) if record.get("detail") is not None else None,
        duration_s=duration,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_failure_records(
    log_path: Path,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> list[dict]:
    """Load raw failure records from *log_path* over the last *days* days.

    Tolerates missing/empty files, malformed lines, and both single-file and
    date-partitioned directory layouts.

    Args:
        log_path: Path to a flat NDJSON file or a directory of dated NDJSON
            files.
        days: How many calendar days to look back from *now*.
        now: Reference timestamp (defaults to ``datetime.now(UTC)``).

    Returns:
        List of raw dicts representing failure events.  Never raises.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    failures: list[dict] = []
    for record in _iter_records_from_path(log_path, days=days, cutoff=cutoff):
        if not _is_failure_record(record):
            continue
        ts = _parse_ts(str(record.get("ts") or ""))
        if ts is not None and ts < cutoff:
            continue
        failures.append(record)
    return failures


def bucket_failures(records: Sequence[dict]) -> dict[FailureSignature, SignatureBucket]:
    """Group failure records by signature ``(persona, phase, error_class)``.

    Malformed records (missing persona or phase) are silently dropped.

    Args:
        records: Iterable of raw log dicts (output of :func:`load_failure_records`
            or any compatible source).

    Returns:
        Dict mapping :class:`FailureSignature` → :class:`SignatureBucket`.
    """
    buckets: dict[FailureSignature, SignatureBucket] = {}
    for record in records:
        trace = _record_to_trace(record)
        if trace is None:
            continue
        error_class = classify_error(trace.detail)
        sig = FailureSignature(
            persona=trace.persona,
            phase=trace.phase,
            error_class=error_class,
        )
        if sig not in buckets:
            buckets[sig] = SignatureBucket(signature=sig)
        buckets[sig].add(trace)
    return buckets


def rank_signatures(
    buckets: dict[FailureSignature, SignatureBucket],
    *,
    min_occurrences: int = 5,
) -> list[SignatureBucket]:
    """Rank buckets by score (frequency * total_cost), filtered by minimum count.

    Only buckets with ``count >= min_occurrences`` are included.  Buckets
    below the threshold are not actionable and are excluded from the result.

    Args:
        buckets: Output of :func:`bucket_failures`.
        min_occurrences: Minimum number of occurrences required to be
            considered actionable (default 5).

    Returns:
        List of :class:`SignatureBucket` sorted descending by score, containing
        only buckets that meet the minimum occurrence threshold.
    """
    actionable = [b for b in buckets.values() if b.count >= min_occurrences]
    return sorted(actionable, key=lambda b: b.score, reverse=True)


def mine(
    log_path: Path,
    *,
    days: int = 30,
    min_occurrences: int = 5,
    now: datetime | None = None,
) -> list[SignatureBucket]:
    """Full mining pipeline: load → bucket → rank.

    Convenience wrapper that composes :func:`load_failure_records`,
    :func:`bucket_failures`, and :func:`rank_signatures`.

    Args:
        log_path: Path to run log (file or directory).
        days: Look-back window in calendar days.
        min_occurrences: Minimum count to be actionable.
        now: Reference time override (for testing).

    Returns:
        Ranked list of actionable :class:`SignatureBucket` objects, or empty
        list when the file is absent/empty or no signature meets the threshold.
    """
    records = load_failure_records(log_path, days=days, now=now)
    buckets = bucket_failures(records)
    return rank_signatures(buckets, min_occurrences=min_occurrences)
