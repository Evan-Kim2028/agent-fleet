"""Parse /agent trigger comments."""

from __future__ import annotations

import re


def extract_persona(body: str, pattern: str) -> str | None:
    match = re.search(pattern, body)
    return match.group(1).strip() if match else None


def is_stop_command(body: str, pattern: str) -> bool:
    return re.search(pattern, body) is not None


def is_watcher_comment(body: str, marker: str) -> bool:
    return marker in body


def extract_issue_number(issue_url: str) -> int | None:
    match = re.search(r"/issues/(\d+)", issue_url)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
