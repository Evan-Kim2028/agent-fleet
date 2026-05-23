"""GitHub API helpers for PR analyzer workflows."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def fetch_pr(repo: str, pr_number: int, token: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected GitHub API response")
    return payload


def post_comment(comment: str, repo: str, pr_number: int, token: str) -> int:
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    payload = json.dumps({"body": comment}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return int(data["id"])


def find_existing_comment(
    repo: str,
    pr_number: int,
    token: str,
    *,
    marker: str,
) -> int | None:
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        comments = json.loads(response.read().decode("utf-8"))
    for comment in comments:
        if marker in str(comment.get("body", "")):
            return int(comment["id"])
    return None


def update_comment(comment_id: int, comment: str, repo: str, token: str) -> None:
    url = f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    payload = json.dumps({"body": comment}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="PATCH")
    urllib.request.urlopen(request, timeout=30)


def load_github_event() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH is required")
    with open(event_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError("invalid GitHub event payload")
    return payload


def resolve_pr_from_event(event: dict[str, Any], repo: str, token: str) -> dict[str, Any]:
    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        return pull_request
    issue = event.get("issue")
    if isinstance(issue, dict) and issue.get("pull_request"):
        return fetch_pr(repo, int(issue["number"]), token)
    raise RuntimeError("must run in pull_request or issue_comment context")


def upsert_pr_comment(
    *,
    repo: str,
    pr_number: int,
    token: str,
    body: str,
    marker: str,
) -> None:
    existing = find_existing_comment(repo, pr_number, token, marker=marker)
    if existing is None:
        post_comment(body, repo, pr_number, token)
        return
    update_comment(existing, body, repo, token)
