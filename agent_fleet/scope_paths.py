"""Repo-relative path matching for persona scope allowlists."""

from __future__ import annotations

from pathlib import Path


def path_under_allowlist(
    path: Path | str,
    allowed_paths: tuple[str, ...] | list[str],
    *,
    worktree: Path | None = None,
) -> bool:
    """Return True if *path* is under one of *allowed_paths* repo-relative prefixes."""
    raw = path.as_posix() if isinstance(path, Path) else str(path).replace("\\", "/")
    if worktree is not None:
        try:
            p = Path(raw)
            if p.is_absolute():
                raw = p.relative_to(worktree.resolve()).as_posix()
        except ValueError:
            pass
    return any(raw.startswith(ap) for ap in allowed_paths)
