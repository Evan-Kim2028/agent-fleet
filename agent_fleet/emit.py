"""Emit seam — single exit point for CLI command output.

``emit(result, fmt="json") -> int`` prints *result* to stdout and returns the
appropriate process exit code based on the result's status, outcome, or verdict
field.  All CLI postambles route through this function so the status→exit-code
mapping lives in one place.

Status→exit-code table
----------------------
Two distinct ok-sets are maintained because two different result shapes exist:

FleetRunResult.outcome (full pipeline, ``fleet run --pipeline full``):
  ok:  completed, completed_noop, review_changes_requested, decompose_partial,
       dag_partial
  not-ok: anything else (error, rejected, scope_violation, …)

FleetTaskResult.status (dispatcher pipeline, default ``fleet run``):
  ok:  completed, merged, decompose_partial, dag_partial
  not-ok: anything else (error, rejected, scope_violation, decompose_failed,
          complexity_underestimated, …)

The two sets are intentionally different:
- ``review_changes_requested`` is ok for the full pipeline (the runner surfaced
  a review request; the task completed and the result is actionable).  It is
  *not* a dispatcher status at all.
- ``completed_noop`` is a full-pipeline outcome (nothing changed); it never
  appears as a dispatcher status.
- ``merged`` is a dispatcher status (PR was merged by the loop); it is not a
  runner outcome.

Do not flatten the two sets.  The distinction is load-bearing for callers that
check exit codes in CI.

Review verdict table (from cmd_review):
  ok:  approve
  not-ok: block, request_changes

Dict with 'error' key present → exit 1.

Plain dicts with none of the above keys → exit 0.

Empty list → exit 1 (no success confirmed).
"""

from __future__ import annotations

import json
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Exit-code tables
# ---------------------------------------------------------------------------

# Full pipeline (FleetRunResult.outcome)
_FULL_PIPELINE_OK: frozenset[str] = frozenset(
    {
        "completed",
        "completed_noop",
        "review_changes_requested",
        "decompose_partial",
        "dag_partial",
    }
)

# Dispatcher pipeline (FleetTaskResult.status)
_DISPATCHER_OK: frozenset[str] = frozenset(
    {
        "completed",
        "merged",
        "decompose_partial",
        "dag_partial",
    }
)

# Review verdict (cmd_review result dict)
_REVIEW_OK: frozenset[str] = frozenset({"approve"})


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


def emit(result: Any, fmt: str = "json") -> int:
    """Print *result* and return an exit code.

    Parameters
    ----------
    result:
        A dict, list-of-dicts, or any object with a ``__dict__`` attribute.
        Dataclasses are serialised via ``default=str`` so Path and datetime
        values don't raise TypeError.
    fmt:
        ``"json"`` (default) — serialise and print as indented JSON.
        ``"comment"`` — print ``result["comment_markdown"]`` as plain text.
        The exit code is still derived from the verdict/status/outcome even
        when ``fmt="comment"``.

    Returns
    -------
    int
        0 on success, 1 on failure.
    """
    code = _exit_code(result)

    if fmt == "comment" and isinstance(result, dict) and "comment_markdown" in result:
        print(result["comment_markdown"])
    else:
        print(json.dumps(result, indent=2, default=str))

    return code


# ---------------------------------------------------------------------------
# Internal: derive exit code from result shape
# ---------------------------------------------------------------------------


def _exit_code(result: Any) -> int:
    """Return 0 or 1 for *result* using the documented status→exit-code tables.

    Dispatch order:
    1. Empty list → 1.
    2. Non-empty list → inspect first element's ``status`` key (dispatcher table).
    3. Dict with ``verdict`` key → review verdict table.
    4. Dict with ``outcome`` key → full-pipeline table.
    5. Dict with ``status`` key → dispatcher table (single-element form).
    6. Dict with ``error`` key (value truthy) → 1.
    7. Fallthrough → 0.
    """
    if isinstance(result, list):
        if not result:
            return 1
        first = result[0]
        status = first.get("status", "") if isinstance(first, dict) else ""
        return 0 if status in _DISPATCHER_OK else 1

    if isinstance(result, dict):
        # Review verdict takes priority over status/outcome.
        if "verdict" in result:
            return 0 if str(result["verdict"]) in _REVIEW_OK else 1
        # Full pipeline: outcome field.
        if "outcome" in result:
            return 0 if str(result["outcome"]) in _FULL_PIPELINE_OK else 1
        # Single dispatcher result: status field.
        if "status" in result:
            return 0 if str(result["status"]) in _DISPATCHER_OK else 1
        # Error key present and truthy → failure.
        if result.get("error"):
            return 1
        return 0

    # Fallthrough for non-dict, non-list (e.g. a string or None)
    return 0
