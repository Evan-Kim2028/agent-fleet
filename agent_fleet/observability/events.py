"""Structured fleet event types."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class RunContext:
    """Execution context propagated through a fleet run."""

    run_id: str
    issue_number: int | None = None
    task_id: int | None = None
    persona: str | None = None
    visual_audit: bool = False
    phase: str | None = None


@dataclass(frozen=True)
class FleetEvent:
    """One machine-readable fleet event."""

    ts: str
    run_id: str
    event: str
    level: str = "info"
    phase: str | None = None
    issue_number: int | None = None
    persona: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(
        cls,
        *,
        run_id: str,
        event: str,
        level: str = "info",
        phase: str | None = None,
        issue_number: int | None = None,
        persona: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> FleetEvent:
        return cls(
            ts=datetime.now(UTC).isoformat(),
            run_id=run_id,
            event=event,
            level=level,
            phase=phase,
            issue_number=issue_number,
            persona=persona,
            data=dict(data or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ts": self.ts,
            "run_id": self.run_id,
            "event": self.event,
            "level": self.level,
        }
        if self.phase is not None:
            payload["phase"] = self.phase
        if self.issue_number is not None:
            payload["issue_number"] = self.issue_number
        if self.persona is not None:
            payload["persona"] = self.persona
        if self.data:
            payload["data"] = self.data
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, ensure_ascii=False)
