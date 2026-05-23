"""GitHub issue operations for issue dispatch (via gh CLI)."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _gh(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        check=False,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result


def repo_full_name(*, cwd: Path | None = None) -> str:
    result = _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", cwd=cwd)
    return result.stdout.strip()


def poll_issue_comments(
    repo: str,
    since: str,
    *,
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    result = _gh(
        "api",
        f"repos/{repo}/issues/comments",
        "-f",
        f"since={since}",
        "--paginate",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    comments: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comments.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return comments


def issue_view(issue_number: int, *, cwd: Path | None = None) -> dict[str, Any]:
    result = _gh(
        "issue",
        "view",
        str(issue_number),
        "--json",
        "title,body,labels,number",
        cwd=cwd,
    )
    return json.loads(result.stdout)


def post_issue_comment(issue_number: int, body: str, *, cwd: Path | None = None) -> None:
    _gh("issue", "comment", str(issue_number), "--body", body, cwd=cwd)


def add_label(issue_number: int, label: str, *, cwd: Path | None = None) -> None:
    _gh("issue", "edit", str(issue_number), "--add-label", label, cwd=cwd, check=False)


def remove_label(issue_number: int, label: str, *, cwd: Path | None = None) -> None:
    _gh("issue", "edit", str(issue_number), "--remove-label", label, cwd=cwd, check=False)


def issue_labels(issue_number: int, *, cwd: Path | None = None) -> list[str]:
    data = issue_view(issue_number, cwd=cwd)
    labels = data.get("labels") or []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            names.append(str(label.get("name", "")))
        else:
            names.append(str(label))
    return [name for name in names if name]
