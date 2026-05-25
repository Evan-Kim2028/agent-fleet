# ruff: noqa: TC003
"""Structured journaling for persona level-up events."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_fleet.level_up.paths import JOURNAL_INDEX_PATH, persona_dir


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def journal_path(repo_key_value: str, persona: str) -> Path:
    return persona_dir(repo_key_value, persona) / "journal.jsonl"


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")


def append_journal(
    event: str,
    repo_key: str,
    persona: str,
    *,
    run_id: str | None = None,
    data: dict[str, Any] | None = None,
    level: str = "info",
) -> dict[str, Any]:
    """Append a journal event to the persona journal and optional global index."""
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": event,
        "repo_key": repo_key,
        "persona": persona,
        "level": level,
        "data": data or {},
    }
    if run_id is not None:
        record["run_id"] = run_id

    _append_jsonl(journal_path(repo_key, persona), record)
    _append_jsonl(JOURNAL_INDEX_PATH, record)
    return record


def tail_journal(repo_key_value: str, persona: str, *, tail: int = 20) -> list[dict[str, Any]]:
    path = journal_path(repo_key_value, persona)
    if not path.is_file() or tail <= 0:
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-tail:]:
        stripped = line.strip()
        if not stripped:
            continue
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            entries.append(payload)
    return entries
