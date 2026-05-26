"""Cron expression helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def format_iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_fire_at(*, cron: str, timezone: str, after: datetime | None = None) -> datetime:
    """Return the next cron fire time strictly after *after* (UTC)."""
    tz = ZoneInfo(timezone)
    base = (after or datetime.now(UTC)).astimezone(tz)
    itr = croniter(cron, base)
    nxt = itr.get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(UTC)


def is_due(*, next_due_at: str | None, now: datetime | None = None) -> bool:
    if not next_due_at:
        return True
    current = now or datetime.now(UTC)
    return parse_iso(next_due_at) <= current
