"""Experience recording for persona level-up training input."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agent_fleet.level_up.models import ExperienceEntry
from agent_fleet.level_up.paths import (
    WEIGHT_DEFAULT,
    WEIGHT_PR_LOOP_ROUND2,
    WEIGHT_REVIEW_FIX_SUCCESS,
    persona_dir,
)


def compute_experience_weight(
    source: str,
    pr_loop_round: int | None = None,
    *,
    status: str | None = None,
) -> float:
    """Return training weight for an experience row."""
    if source == "pr_loop" and pr_loop_round is not None and pr_loop_round >= 2:
        return WEIGHT_PR_LOOP_ROUND2
    if (
        source == "pr_loop"
        and status == "completed"
        and pr_loop_round is not None
        and pr_loop_round >= 1
    ):
        return WEIGHT_REVIEW_FIX_SUCCESS
    return WEIGHT_DEFAULT


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def read_experience_rows(
    repo_key_value: str,
    persona: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    experience_path = persona_dir(repo_key_value, persona) / "experience.jsonl"
    if not experience_path.is_file():
        return []
    lines = [
        line.strip()
        for line in experience_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit is not None and limit > 0:
        lines = lines[-limit:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def read_last_experience(repo_key_value: str, persona: str) -> dict[str, Any] | None:
    rows = read_experience_rows(repo_key_value, persona, limit=1)
    return rows[-1] if rows else None


def last_experience_shows_verify_failed(repo_key_value: str, persona: str) -> bool:
    last = read_last_experience(repo_key_value, persona)
    if last is None:
        return False
    status = str(last.get("status") or "")
    if status == "verify_failed":
        return True
    note = str(last.get("note") or "")
    return "verify_failed" in note


def append_experience(
    *,
    repo_key: str,
    persona: str,
    source: str,
    weight: float = WEIGHT_DEFAULT,
    pr_loop_round: int | None = None,
    status: str | None = None,
    goal: str | None = None,
    review_verdict: str | None = None,
    equip_snapshot: dict[str, Any] | None = None,
    changed_files: list[str] | tuple[str, ...] | None = None,
    run_id: str | None = None,
) -> ExperienceEntry:
    """Append one experience row to persona experience.jsonl."""
    entry = ExperienceEntry(
        source=source,
        weight=weight,
        pr_loop_round=pr_loop_round,
        status=status,
        goal=goal,
        review_verdict=review_verdict,
        equip_snapshot=dict(equip_snapshot or {}),
        changed_files=tuple(changed_files or ()),
        run_id=run_id,
        repo_key=repo_key,
        persona=persona,
    )

    record: dict[str, Any] = {
        "ts": _now_iso(),
        "source": entry.source,
        "weight": entry.weight,
        "repo_key": entry.repo_key,
        "persona": entry.persona,
    }
    if entry.pr_loop_round is not None:
        record["pr_loop_round"] = entry.pr_loop_round
    if entry.status is not None:
        record["status"] = entry.status
    if entry.goal is not None:
        record["goal"] = entry.goal
    if entry.review_verdict is not None:
        record["review_verdict"] = entry.review_verdict
    if entry.equip_snapshot:
        record["equip_snapshot"] = entry.equip_snapshot
    if entry.changed_files:
        record["changed_files"] = list(entry.changed_files)
    if entry.run_id is not None:
        record["run_id"] = entry.run_id

    path = persona_dir(repo_key, persona) / "experience.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")

    return entry
