"""Skill synthesis engine for the self-improving flywheel.

This is the core of the "agent orchestrator updates skills" capability.

Design goals:
- Operates on the central ~/.agent-fleet/ store (cross-repo)
- Can be triggered by the dispatcher / orchestrator (not only CLI)
- Produces high-quality candidate skills for the _fleet tier
- Reuses the existing level_up gate + promotion machinery
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_fleet.level_up.paths import FLEET_TIER, LEVEL_UP_ROOT
from agent_fleet.level_up.train import train_persona


@dataclass
class FleetSynthesisResult:
    personas_updated: list[str]
    new_rules_proposed: int
    promoted_to_fleet: int
    details: dict[str, Any]


def synthesize_fleet_skills(
    *,
    personas: list[str] | None = None,
    min_experience_rows: int = 20,
    dry_run: bool = False,
) -> FleetSynthesisResult:
    """
    Run cross-repo skill synthesis for the global fleet tier.

    This is intended to be callable from:
    - CLI (agent-fleet learn)
    - Dispatcher / background maintenance loop
    - A special "fleet-learner" persona run

    Currently this is a thin wrapper that leverages the per-persona
    train_persona machinery but biases toward contributing to _fleet.
    """
    if personas is None:
        # Default personas that make sense to evolve at fleet level
        personas = ["coder", "reviewer", "pr-analyzer"]

    updated: list[str] = []
    total_proposed = 0
    total_promoted = 0

    for persona in personas:
        # Also look across all repo keys for this persona
        total_rows = 0
        for repo_dir in LEVEL_UP_ROOT.iterdir():
            if repo_dir.name == FLEET_TIER:
                continue
            exp_file = repo_dir / persona / "experience.jsonl"
            if exp_file.exists():
                lines = [line for line in exp_file.read_text().splitlines() if line.strip()]
                total_rows += len(lines)

        if total_rows < min_experience_rows:
            continue

        # Use the existing train machinery, forcing contribution to fleet
        result = train_persona(
            repo_key=FLEET_TIER,
            persona=persona,
            contribute_to_fleet=True,
            dry_run=dry_run,
        )

        if result.promoted or result.queued:
            updated.append(persona)
            total_proposed += len(result.queued) + len(result.promoted)
            total_promoted += len(result.promoted)

    return FleetSynthesisResult(
        personas_updated=updated,
        new_rules_proposed=total_proposed,
        promoted_to_fleet=total_promoted,
        details={"min_experience_rows": min_experience_rows},
    )


def trigger_fleet_learning_cycle(
    *,
    personas: list[str] | None = None,
    dry_run: bool = False,
) -> FleetSynthesisResult:
    """
    Entry point designed to be called by the FleetDispatcher or background
    maintenance loops.

    This is how the orchestrator itself can drive the self-improving flywheel
    without requiring a human to run `agent-fleet learn`.
    """
    return synthesize_fleet_skills(
        personas=personas,
        dry_run=dry_run,
    )
