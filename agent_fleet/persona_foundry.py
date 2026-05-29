"""Auto-generate personas on demand so the fleet is not limited to the fixed persona set."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from agent_fleet.hooks import LLMBackend
    from agent_fleet.observability.fleet_logger import FleetLogger

_logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-z0-9_-]")
_MAX_NAME_LEN = 64
_MAX_BODY_LEN = 16384

# Class-level lock protecting _name_locks dict
_locks_mu: threading.Lock = threading.Lock()
_name_locks: dict[str, threading.Lock] = {}

_history_mu: threading.Lock = threading.Lock()

_GENERATION_PROMPT = """\
Produce ONLY the markdown body (no YAML front-matter, no code fences) for a \
persona named "{name}". Include exactly these four sections in order, each as a \
level-2 heading:

## Role
## Expertise
## Scope discipline
## Methodology

Each section should be 2-4 concise sentences describing a specialist named "{name}".\
"""


class PersonaGenerationError(Exception):
    pass


def _atomic_write(dest: Path, text: str) -> None:
    """Write *text* to *dest* atomically via a sibling temp file."""
    fd, tmp_str = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        shutil.move(tmp_str, dest)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class PersonaFoundry:
    def __init__(
        self,
        *,
        personas_dir: Path,
        backend: LLMBackend,
        model: str,
        fleet_log: FleetLogger | None = None,
    ) -> None:
        self._personas_dir = personas_dir
        self._backend = backend
        self._model = model
        self._fleet_log = fleet_log

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """Lowercase and strip to [a-z0-9_-], max 64 chars.

        Raises PersonaGenerationError for empty results or path-traversal
        patterns — security boundary: name originates from planner LLM output.
        """
        if "/" in name or "\\" in name or ".." in name:
            raise PersonaGenerationError(
                f"Persona name contains path-traversal characters: {name!r}"
            )
        safe = _SAFE_NAME_RE.sub("", name.lower())[:_MAX_NAME_LEN]
        if not safe:
            raise PersonaGenerationError(
                f"Persona name {name!r} is empty after sanitization"
            )
        return safe

    @staticmethod
    def _validate_markdown(text: str) -> None:
        if not text or not text.strip():
            raise PersonaGenerationError("Generated persona body is empty")
        if len(text) >= _MAX_BODY_LEN:
            raise PersonaGenerationError(
                f"Generated persona body exceeds {_MAX_BODY_LEN} chars (got {len(text)})"
            )
        if "## " not in text:
            raise PersonaGenerationError(
                "Generated persona body contains no '## ' headings"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, name: str) -> None:
        """Synthesize and write a persona file for *name*.

        Idempotent: returns immediately if the .md already exists.
        Thread-safe: a per-name lock prevents duplicate backend calls.
        """
        safe = self._sanitize_name(name)
        md_path = self._personas_dir / f"{safe}.md"

        if md_path.exists():
            return

        with _locks_mu:
            lock = _name_locks.setdefault(safe, threading.Lock())

        with lock:
            if md_path.exists():
                return

            result = self._backend.run(
                _GENERATION_PROMPT.format(name=safe),
                max_tokens=2048,
                timeout_s=120,
                model=self._model,
            )
            body = result.stdout

            self._validate_markdown(body)

            self._personas_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(md_path, body)

            loadout: dict[str, object] = {
                "schema_version": 1,
                "stub": f"{safe}.md",
                "skills": {"execute": []},
                "pipeline_skills": {"code_review": {"review": []}},
            }
            _atomic_write(
                self._personas_dir / f"{safe}.loadout.yaml",
                yaml.safe_dump(loadout, sort_keys=False),
            )

            now = datetime.now(UTC)
            ts_stamp = now.strftime("%Y%m%dT%H%M%SZ")
            ts_iso = now.isoformat()
            archive_name = f"{safe}.{ts_stamp}.md"
            history_dir = self._personas_dir / ".foundry_history"
            history_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(history_dir / archive_name, body)
            row = json.dumps(
                {
                    "name": safe,
                    "ts": ts_iso,
                    "model": self._model,
                    "chars": len(body),
                    "archive": archive_name,
                },
                separators=(",", ":"),
            )
            registry = self._personas_dir / ".foundry_history.jsonl"
            with _history_mu, registry.open("a", encoding="utf-8") as fh:
                fh.write(row + "\n")

        if self._fleet_log is not None:
            self._fleet_log.emit("orchestration.persona_generated", name=safe)

    def resolve_or_generate(
        self,
        name: str,
        known: set[str],
        fallback: str,
    ) -> str:
        """Return *name* if known; generate it; or fall back to *fallback* on error."""
        if name in known:
            return name
        try:
            safe = self._sanitize_name(name)
            self.generate(name)
            return safe
        except Exception as exc:
            _logger.warning(
                "PersonaFoundry: could not generate persona %r, using fallback %r: %s",
                name,
                fallback,
                exc,
            )
            return fallback
