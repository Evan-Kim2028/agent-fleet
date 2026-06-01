"""VisualCapture Protocol and CaptureArtifact for design-review adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CaptureArtifact:
    """A single visual capture artifact produced by a ``VisualCapture`` impl."""

    viewport: str
    state: str
    ref: str
    image_path: Path


@runtime_checkable
class VisualCapture(Protocol):
    """Capture visual screenshots of changed UI surfaces."""

    def capture(
        self,
        changed_files: list[str],
        *,
        workdir: Path,
    ) -> list[CaptureArtifact]:
        ...
