"""Outer retry loop reacting to hard task failures with curated handoff."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from agent_fleet.contracts.handoff import HandoffNote

if TYPE_CHECKING:
    from collections.abc import Callable

_HARD_STATUSES = frozenset(
    {"error", "cancelled", "expired", "timeout", "scope_violation", "pipeline_nonzero"}
)


class _ResultLike(Protocol):
    status: str


def _is_hard_failure(result: _ResultLike) -> bool:
    status = getattr(result, "status", "")
    exit_code = getattr(result, "exit_code", 0)
    return status in _HARD_STATUSES or exit_code not in (0,)


def _extract_handoff(result: _ResultLike, *, previous: HandoffNote | None) -> HandoffNote:
    status = getattr(result, "status", "error")
    files = tuple(getattr(result, "files_modified", ()) or ())
    stderr = str(getattr(result, "stderr", "") or "")
    if not stderr:
        stderr = str(getattr(result, "error", "") or "")
    changed = getattr(result, "changed_files", None)
    if not files and changed:
        files = tuple(str(p) for p in changed)
    attempt = (previous.attempt_number + 1) if previous else 1
    summary = (
        f"Previous attempt ended with status={status!r}. "
        f"Modified {len(files)} file(s) before reset. "
        "Do not repeat the same approach blindly; analyze the stderr above "
        "and plan around the failure mode."
    )
    return HandoffNote(
        failure_mode=status,
        files_touched=files,
        stderr_tail=stderr,
        summary=summary,
        attempt_number=attempt,
        previous=previous,
    )


def dispatch_with_retry[T: _ResultLike](
    task: object,
    *,
    dispatch: Callable[..., T],
    max_redispatches: int = 1,
    on_event: Callable[[str, dict[str, object]], None] | None = None,
) -> T:
    handoff: HandoffNote | None = None
    result: T | None = None
    for attempt in range(max_redispatches + 1):
        if on_event is not None:
            on_event(
                "redispatch.attempt",
                {"attempt": attempt, "has_handoff": handoff is not None},
            )
        result = dispatch(task, handoff=handoff)
        if not _is_hard_failure(result):
            return result
        if attempt == max_redispatches:
            break
        handoff = _extract_handoff(result, previous=handoff)
    assert result is not None
    return result
