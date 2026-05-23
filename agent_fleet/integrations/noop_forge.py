"""No-op forge for local-only runs without PR integration."""

from __future__ import annotations


class NoOpForge:
    def open_pr(self, **kwargs: object) -> int:
        del kwargs
        return 0

    def mark_ready(self, pr_number: int) -> None:
        del pr_number

    def comment(self, issue_or_pr: int, body: str) -> None:
        del issue_or_pr, body

    def get_labels(self, issue_or_pr: int) -> list[str]:
        del issue_or_pr
        return []
