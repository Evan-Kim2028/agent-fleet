"""Outer retry loop reacting to hard task failures with curated handoff."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from agent_fleet.contracts.handoff import HandoffNote

if TYPE_CHECKING:
    from collections.abc import Callable

_HARD_STATUSES = frozenset(
    {
        "error",
        "cancelled",
        "expired",
        "timeout",
        "scope_violation",
        "token_ceiling_exceeded",
    }
)

# scope_violation and token_ceiling_exceeded are deterministic outcomes: retrying the
# same task into the same scope constraint or token ceiling produces the same result.
# Never retry these.
_TERMINAL_STATUSES = frozenset({"scope_violation", "token_ceiling_exceeded"})


@dataclass
class RetryPolicy:
    """Decides whether a failed attempt should be retried.

    ``max_attempts`` is the total number of tries (initial + retries), matching
    ``max_redispatches + 1`` from the legacy flat-int configuration.

    Note: this is NOT an exact reproduction of the legacy behavior. Terminal
    statuses (``scope_violation`` and ``token_ceiling_exceeded``) are never
    retried regardless of budget — an intentional deviation from the legacy
    flat-int which had no such exemption.
    """

    max_attempts: int = 2
    # Statuses that are never retried regardless of budget.
    terminal_statuses: frozenset[str] = field(default_factory=lambda: _TERMINAL_STATUSES)

    def should_retry(self, result: _ResultLike, attempt: int) -> bool:
        """Return True if ``result`` from ``attempt`` (0-based) should be retried.

        ``attempt`` is the 0-based index of the attempt just completed, so
        attempt 0 is the first try, attempt 1 is the first retry, etc.
        """
        status = getattr(result, "status", "")
        if status in self.terminal_statuses:
            return False
        if not _is_hard_failure(result):
            return False
        # attempt is 0-based; max_attempts includes the initial run.
        return attempt + 1 < self.max_attempts

    @classmethod
    def from_max_redispatches(cls, max_redispatches: int) -> RetryPolicy:
        """Build a policy from the legacy ``max_redispatches`` int.

        The attempt budget matches the legacy value (``max_redispatches + 1``
        total tries), but terminal statuses are never retried — an intentional
        deviation that the legacy flat-int did not enforce.
        """
        return cls(max_attempts=max_redispatches + 1)


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
    policy: RetryPolicy | None = None,
    on_event: Callable[[str, dict[str, object]], None] | None = None,
) -> T:
    effective_policy = policy or RetryPolicy.from_max_redispatches(max_redispatches)
    handoff: HandoffNote | None = None
    result: T | None = None
    for attempt in range(effective_policy.max_attempts):
        if on_event is not None:
            on_event(
                "redispatch.attempt",
                {"attempt": attempt, "has_handoff": handoff is not None},
            )
        result = dispatch(task, handoff=handoff)
        if not effective_policy.should_retry(result, attempt):
            return result
        handoff = _extract_handoff(result, previous=handoff)
    assert result is not None
    return result
