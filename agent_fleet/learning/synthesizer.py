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

from agent_fleet.learning.llm_synthesis import get_synthesis_context
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
    backend: Any = None,  # noqa: ANN401, ARG001
    resolver: Any = None,  # noqa: ANN401, ARG001
    fleet_config: Any = None,  # noqa: ANN401, ARG001
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

        # === Real LLM synthesis path ===
        # Delegate to fleet-learner persona (dispatched via normal mechanisms or
        # superpowers:subagent-driven-development). Use the helper for context.
        try:
            context = get_synthesis_context(persona, max_rows=120)
            # In a full implementation, dispatch the fleet-learner persona here
            # with a goal built from context["summary"] + context["recent_samples"].
            # For now this is a no-op placeholder — the persona + existing level_up
            # machinery does the real work when invoked properly.
            _ = context  # consumed by caller when dispatching the persona
        except Exception:
            pass

        # Legacy high-signal hardcoded rules (still valuable)
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
    Entry point for the dispatcher / background loops to drive the flywheel.

    In practice, the best way to run synthesis is to dispatch the `fleet-learner`
    persona (using subagent-driven-development patterns) against ~/.agent-fleet/
    with rich context from get_synthesis_context().

    This thin wrapper still exists for the simple legacy path.
    """
    return synthesize_fleet_skills(
        personas=personas,
        dry_run=dry_run,
    )
