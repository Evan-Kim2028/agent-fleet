"""GitHub issue operations for issue dispatch (via gh CLI)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from agent_fleet.integrations.github_cli import gh as _gh

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def repo_full_name(*, cwd: Path | None = None) -> str:
    result = _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", cwd=cwd)
    return result.stdout.strip()


def flatten_issue_comment_pages(pages: list[Any]) -> list[dict[str, Any]]:
    """Flatten gh api --paginate --slurp output into issue comment dicts."""
    comments: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            comments.extend(item for item in page if isinstance(item, dict))
    return comments


def parse_paginated_json_arrays(stdout: str) -> list[Any]:
    """Parse one or more JSON arrays/objects emitted by gh api --paginate."""
    text = stdout.strip()
    if not text:
        return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, list):
        if payload and all(isinstance(page, list) for page in payload):
            return payload
        if payload and all(isinstance(item, dict) for item in payload):
            return [payload]
        return payload

    values: list[Any] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        rest = text[idx:].lstrip()
        if not rest:
            break
        value, end = decoder.raw_decode(rest)
        values.append(value)
        idx += len(text[idx:]) - len(rest) + end
    return values


def as_comment_pages(values: list[Any]) -> list[list[Any]]:
    """Normalize gh paginate output into a list of comment pages."""
    if not values:
        return []
    if isinstance(values[0], list):
        return [page for page in values if isinstance(page, list)]
    if isinstance(values[0], dict):
        return [values]
    return []


def poll_issue_comments(
    repo: str,
    since: str,
    *,
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    # gh api -f since=... does not apply correctly to this REST endpoint; use query param.
    endpoint = f"repos/{repo}/issues/comments?since={since}"
    result = _gh(
        "api",
        endpoint,
        "--paginate",
        "--slurp",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        logger.debug(
            "poll_issue_comments failed: rc=%s stderr=%s",
            result.returncode,
            result.stderr.strip(),
        )
        return []

    try:
        pages = parse_paginated_json_arrays(result.stdout)
    except json.JSONDecodeError:
        logger.debug("poll_issue_comments: invalid JSON in gh output")
        return []

    if pages and isinstance(pages[0], dict) and pages[0].get("message"):
        logger.debug("poll_issue_comments API error: %s", pages[0].get("message"))
        return []

    return flatten_issue_comment_pages(as_comment_pages(pages))


def issue_view(issue_number: int, *, cwd: Path | None = None) -> dict[str, Any]:
    result = _gh(
        "issue",
        "view",
        str(issue_number),
        "--json",
        "title,body,labels,number,state",
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
