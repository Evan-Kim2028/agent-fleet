"""Path scope checks for fleet runs."""

from __future__ import annotations


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
        if not any(path.startswith(prefix) for prefix in allowed_paths)
    )
