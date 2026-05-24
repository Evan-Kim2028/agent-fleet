"""Structured summary fed into a redispatched task's planner."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HandoffNote:
    failure_mode: str
    files_touched: tuple[str, ...]
    stderr_tail: str
    summary: str
    attempt_number: int = 1
    previous: HandoffNote | None = None

    def render(self) -> str:
        prior = (
            f"\n\n(This is attempt #{self.attempt_number + 1}; prior attempts also failed.)"
            if self.previous
            else ""
        )
        files = (
            "Files modified before reset: " + ", ".join(self.files_touched)
            if self.files_touched
            else "No files were modified."
        )
        return (
            "PREVIOUS ATTEMPT CONTEXT — read carefully before planning.\n"
            f"Failure mode: {self.failure_mode}\n"
            f"{files}\n"
            f"Last stderr (truncated): {self.stderr_tail[-500:]}\n"
            f"Summary: {self.summary}"
            f"{prior}"
        )
