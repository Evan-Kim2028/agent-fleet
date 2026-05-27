"""Canvas path resolution (mirrors cookbook dag-task-runner conventions)."""

from __future__ import annotations

import re
from pathlib import Path


def default_canvases_dir(workspace: Path) -> Path:
    """Return ~/.cursor/projects/<workspace-slug>/canvases for the repo."""
    resolved = workspace.resolve()
    slug = resolved.as_posix().lstrip("/")
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug.replace("/", "-"))
    return Path.home() / ".cursor" / "projects" / slug / "canvases"


def resolve_canvas_path(
    *,
    workspace: Path,
    canvas_path: str | None = None,
    canvas: str | None = None,
    canvases_dir: str | None = None,
) -> Path:
    """Resolve a `.canvas.tsx` path from CLI flags."""
    if canvas_path:
        path = Path(canvas_path).expanduser()
        if not str(path).endswith(".canvas.tsx"):
            path = Path(str(path).removesuffix(".tsx") + ".canvas.tsx")
        return path.resolve()

    if not canvas:
        raise ValueError("Provide --canvas-path or --canvas <name>")

    base = Path(canvases_dir).expanduser() if canvases_dir else default_canvases_dir(workspace)
    stem = canvas.removesuffix(".canvas.tsx")
    return (base / f"{stem}.canvas.tsx").resolve()
