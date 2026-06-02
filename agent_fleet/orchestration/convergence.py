"""Unified output convergence with bounded parent-facing summaries."""

# ruff: noqa: TC001

from __future__ import annotations

from agent_fleet.hooks import FleetTaskResult

SUCCESS_STATUSES = frozenset({"completed", "merged"})
PARTIAL_OK = frozenset({"review_changes_requested"})
FAILURE_STATUSES = frozenset(
    {
        "error",
        "verify_failed",
        "scope_violation",
        "review_blocked",
        "rejected",
        "token_ceiling_exceeded",
    }
)

_TERMINAL = SUCCESS_STATUSES | PARTIAL_OK | {"skipped"}


def is_success(status: str) -> bool:
    return status in SUCCESS_STATUSES | PARTIAL_OK


def is_failure(status: str) -> bool:
    return status in FAILURE_STATUSES or status not in _TERMINAL


def roll_up_status(results: list[FleetTaskResult]) -> str:
    if not results:
        return "failure"
    successes = sum(1 for r in results if r.status in SUCCESS_STATUSES)
    partial = sum(1 for r in results if r.status in PARTIAL_OK)
    if successes + partial == len(results):
        return "success"
    if successes > 0 or partial > 0:
        return "partial"
    return "failure"


def compact_summary(results: list[FleetTaskResult], *, total_chars: int = 400) -> str:
    if not results:
        return ""

    successes = sum(1 for r in results if r.status in SUCCESS_STATUSES)
    partial = sum(1 for r in results if r.status in PARTIAL_OK)
    failures = [r for r in results if r.status not in SUCCESS_STATUSES | PARTIAL_OK]

    if not failures:
        line = f"{successes + partial}/{len(results)} completed"
        if partial:
            line = f"{successes}/{len(results)} completed, {partial} review pending"
        return line[:total_chars]

    parts: list[str] = [f"{successes + partial}/{len(results)} completed, {len(failures)} failed"]
    for result in failures[:3]:
        err = (result.error or result.status)[:100]
        parts.append(f"- {result.goal[:60]}: {err}")
    summary = "\n".join(parts)
    if len(summary) > total_chars:
        return summary[: max(0, total_chars - 3)].rstrip() + "..."
    return summary


def aggregate(
    results: list[FleetTaskResult],
    *,
    max_chars_per_child: int = 400,
) -> tuple[str, str | None, str]:
    """Return (roll_up_status, error-or-None, bounded summary)."""
    summary = compact_summary(results, total_chars=max_chars_per_child)
    status = roll_up_status(results)
    if status == "success":
        return status, None, summary
    if status == "partial":
        failed = [r for r in results if r.status not in SUCCESS_STATUSES | PARTIAL_OK]
        err = "; ".join(f"{r.goal[:40]}: {r.error or r.status}" for r in failed[:3])
        return status, err or "Some tasks failed", summary
    err = results[0].error or results[0].status if results else "failed"
    return status, err, summary


def budget_upstream_context(
    outputs: dict[str, str],
    dep_ids: tuple[str, ...] | list[str],
    *,
    total_budget: int = 2000,
) -> str:
    """Build upstream body with total char budget split across dependencies."""
    if not dep_ids:
        return ""

    n = len(dep_ids)
    header_reserve = 24 * n + 32
    snippet_budget = max(1, total_budget - header_reserve)
    per_dep = max(1, snippet_budget // n)

    parts: list[str] = []
    for parent_id in dep_ids:
        snippet = outputs.get(parent_id, "").strip()
        if not snippet:
            parts.append(f"### {parent_id}\n(no output recorded)")
            continue
        if len(snippet) > per_dep:
            snippet = snippet[: max(0, per_dep - 3)].rstrip() + "..."
        parts.append(f"### {parent_id}\n{snippet}")
    return "\n\n".join(parts)
