"""Cross-repo experience aggregation for the self-improving flywheel.

The central ~/.agent-fleet/ directory is the single source of truth for
fleet-wide learning.
"""

from __future__ import annotations

from typing import Any

from agent_fleet.level_up.paths import LEVEL_UP_ROOT


def aggregate_fleet_experience(
    personas: list[str] | None = None,
    limit_per_persona: int = 500,
) -> dict[str, list[dict[str, Any]]]:
    """
    Collect recent experience rows across all repositories + the _fleet tier
    for the given personas.

    This gives the meta-learner (orchestrator) a global view instead of
    per-repo myopia.
    """
    if personas is None:
        personas = ["coder", "reviewer", "pr-analyzer"]

    result: dict[str, list[dict[str, Any]]] = {p: [] for p in personas}

    for persona in personas:
        rows: list[dict[str, Any]] = []

        # Walk the entire level_up tree
        for repo_dir in LEVEL_UP_ROOT.iterdir():
            if not repo_dir.is_dir():
                continue
            exp_file = repo_dir / persona / "experience.jsonl"
            if not exp_file.exists():
                continue

            for line in exp_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    import json
                    row = json.loads(line)
                    row["_source_repo"] = repo_dir.name
                    rows.append(row)
                except Exception:
                    continue

        # Keep most recent
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        result[persona] = rows[:limit_per_persona]

    return result


def get_fleet_experience_summary(persona: str, max_rows: int = 100) -> str:
    """Produce a compact text summary suitable for feeding to an LLM meta-agent."""
    data = aggregate_fleet_experience([persona], limit_per_persona=max_rows)
    rows = data.get(persona, [])

    if not rows:
        return f"No experience recorded yet for persona '{persona}'."

    lines = [f"Recent experience for {persona} (most recent first):"]
    for row in rows[:30]:  # keep prompt reasonable
        status = row.get("status")
        source = row.get("_source_repo", "?")
        goal = str(row.get("goal", ""))[:80]
        lines.append(f"- [{source}] status={status} goal={goal}")

    lines.append(f"\nTotal recent rows considered: {len(rows)}")
    return "\n".join(lines)
