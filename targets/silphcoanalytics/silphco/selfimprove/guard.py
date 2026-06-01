"""guard.py — Hard path allowlist/denylist for the self-improvement loop.

The loop may ONLY propose edits to files that match an explicit allowlist of
glob patterns.  A separate denylist provides a second safety layer; denylist
always wins over allowlist when both apply.

Design principles
-----------------
* **Denylist wins ties** — if a path matches both allow and deny, it is
  denied.  This makes the guard fail-closed.
* **No path traversal** — ``../`` and absolute paths that escape the repo are
  unconditionally denied before any glob matching.
* **Symlinks are not resolved** — the guard operates on the literal string
  path provided by the proposer.  The caller must pass relative paths (relative
  to the repository root).
* **All matching is case-sensitive** (POSIX paths).

Allowlist globs (may ONLY edit these):
    agents/personas/*.md
    agents/pr_review_overlay.md

Denylist patterns (hard block — NEVER edit these):
    agents/silphco/selfimprove/guard.py
    agents/silphco/selfimprove/mine.py
    agents/silphco/selfimprove/gate.py
    agents/silphco/selfimprove/loop.py
    agents/agents/**
    agents/silphco/**
    .agent-fleet.yaml
    .github/**
    agents/tests/**
    **/test_*.py
    **/*_test.py
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath


# ---------------------------------------------------------------------------
# Glob patterns — edit these to grow/shrink the loop's edit rights.
# ---------------------------------------------------------------------------

#: Paths the loop is ALLOWED to edit.  Only pure-text prompt/persona files.
ALLOW_GLOBS: tuple[str, ...] = (
    "agents/personas/*.md",
    "agents/pr_review_overlay.md",
)

#: Paths the loop may NEVER edit.  Denylist wins over allowlist.
DENY_GLOBS: tuple[str, ...] = (
    # Self-modification of safety machinery
    "agents/silphco/selfimprove/guard.py",
    "agents/silphco/selfimprove/mine.py",
    "agents/silphco/selfimprove/gate.py",
    "agents/silphco/selfimprove/loop.py",
    "agents/silphco/selfimprove/__main__.py",
    # Dispatcher, verifier, and silphco internals
    "agents/agents/**",
    "agents/silphco/**",
    # Fleet package config (installed dependency, not repo paths)
    ".agent-fleet.yaml",
    ".github/**",
    # Tests — must not be self-modified
    "agents/tests/**",
    "**/test_*.py",
    "**/*_test.py",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise(path: str) -> str | None:
    """Normalise *path* to a clean POSIX relative string.

    Returns ``None`` if the path contains path-traversal sequences
    (``..``), is absolute, or is otherwise suspicious.
    """
    # Reject absolute paths
    if path.startswith("/") or path.startswith("\\"):
        return None
    # Normalise separators and collapse redundant slashes
    try:
        p = PurePosixPath(path)
    except (TypeError, ValueError):
        return None
    normalised = str(p)
    # Reject traversal
    for part in p.parts:
        if part == "..":
            return None
    # Reject if normalised starts with / (PurePosixPath can produce this for
    # strings like '//foo' after collapse — extra safety net)
    if normalised.startswith("/"):
        return None
    return normalised


def _matches_any(path: str, globs: tuple[str, ...]) -> bool:
    """Return True if *path* matches any of the given fnmatch *globs*."""
    for pattern in globs:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_allowed(path: str) -> bool:
    """Return True only when *path* passes the guard.

    A path passes iff:
    1. It is a valid, non-traversal relative POSIX path.
    2. It matches at least one allowlist glob.
    3. It does NOT match any denylist glob (denylist wins).

    Args:
        path: Relative path from repository root (e.g.
            ``"agents/personas/backend.md"``).

    Returns:
        ``True`` when the path is permitted; ``False`` otherwise.
    """
    normalised = _normalise(path)
    if normalised is None:
        return False
    if _matches_any(normalised, DENY_GLOBS):
        return False
    return _matches_any(normalised, ALLOW_GLOBS)


def check(path: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for *path*.

    Provides a human-readable reason for debugging and audit logs.

    Args:
        path: Relative path from repository root.

    Returns:
        ``(True, "")`` when the path is allowed.
        ``(False, reason_string)`` when denied.
    """
    normalised = _normalise(path)
    if normalised is None:
        return False, f"path traversal or absolute path rejected: {path!r}"
    if _matches_any(normalised, DENY_GLOBS):
        return False, f"path matches denylist: {normalised!r}"
    if not _matches_any(normalised, ALLOW_GLOBS):
        return False, f"path does not match any allowlist glob: {normalised!r}"
    return True, ""
