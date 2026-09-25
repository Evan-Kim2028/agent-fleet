"""JSON file state persistence shared by fleet watchers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class JsonStateStore:
    """Load/save a JSON object at a fixed path."""

    def __init__(self, path: Path, *, atomic: bool = False) -> None:
        self.path = path
        self.atomic = atomic

    def load(self, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
        base = dict(defaults or {})
        if not self.path.exists():
            return base
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return base
        if not isinstance(data, dict):
            return base
        return {**base, **data}

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2)
        if self.atomic:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.path)
        else:
            self.path.write_text(payload, encoding="utf-8")
