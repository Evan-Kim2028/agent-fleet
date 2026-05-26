"""Path scope checks for fleet runs."""

from __future__ import annotations


def _normalize_scope_path(path: str) -> str:
    return path.strip().rstrip("/")


def path_allowed_by_prefix(changed: str, prefix: str) -> bool:
    """True when *changed* is the same path, under *prefix*, or a parent directory of it."""
    changed_n = _normalize_scope_path(changed)
    prefix_n = _normalize_scope_path(prefix)
    if not prefix_n:
        return True
    if not changed_n:
        return True
    if changed_n == prefix_n:
        return True
    if changed_n.startswith(f"{prefix_n}/"):
        return True
    if prefix_n.startswith(f"{changed_n}/"):
        return True
    return changed_n.startswith(prefix_n)


def files_outside_allowed_paths(
    allowed_paths: tuple[str, ...] | list[str],
    changed_files: list[str],
) -> tuple[str, ...]:
    """Return changed files that fall outside *allowed_paths* prefixes.

    Empty *allowed_paths* means unrestricted scope (returns empty tuple).
    """
    if not allowed_paths:
        return ()
    return tuple(
        path
        for path in changed_files
        if not any(path_allowed_by_prefix(path, prefix) for prefix in allowed_paths)
    )
