"""GitHub forge integration for push + PR open."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class GitHubForge:
    """Concrete GitForge using gh CLI."""

    def __init__(self, *, cwd: Path | None = None) -> None:
        self.cwd = cwd

    def _gh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["gh", *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(self.cwd) if self.cwd else None,
            check=False,
            timeout=120,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return result

    def open_pr(
        self,
        *,
        title: str,
        body: str,
        branch: str,
        base: str = "main",
        draft: bool = False,
        labels: list[str] | None = None,
    ) -> int:
        cmd = [
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            cmd.append("--draft")
        try:
            result = self._gh(*cmd)
            pr_url = result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            existing = re.search(
                r"already exists:.*?(https://\S+/pull/(\d+))",
                exc.stderr or "",
            )
            if existing:
                return int(existing.group(2))
            raise RuntimeError(f"gh pr create failed: {exc.stderr.strip()}") from exc

        match = re.search(r"/pull/(\d+)", pr_url)
        if not match:
            raise RuntimeError(f"Could not parse PR number from: {pr_url!r}")
        pr_number = int(match.group(1))

        for label in labels or []:
            self._ensure_label(label)
            self._gh("pr", "edit", str(pr_number), "--add-label", label, check=False)

        return pr_number

    def mark_ready(self, pr_number: int) -> None:
        self._gh("pr", "ready", str(pr_number), check=False)

    def comment(self, issue_or_pr: int, body: str) -> None:
        self._gh("issue", "comment", str(issue_or_pr), "--body", body)

    def get_labels(self, issue_or_pr: int) -> list[str]:
        result = self._gh(
            "issue",
            "view",
            str(issue_or_pr),
            "--json",
            "labels",
            check=False,
        )
        if result.returncode != 0:
            return []
        labels = json.loads(result.stdout).get("labels") or []
        return [str(label.get("name", "")) for label in labels if label.get("name")]

    def _ensure_label(self, label: str) -> None:
        self._gh("label", "create", label, "--force", check=False)
