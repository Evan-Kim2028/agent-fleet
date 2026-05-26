"""Thin adapter for LLM skill synthesis in the flywheel.

**LESS IS MORE + USE SUPERPOWERS:**

The real synthesis should be done by dispatching the `fleet-learner` persona
(using the project's normal dispatcher or subagent-driven-development patterns)
against the `~/.agent-fleet/` workspace, with a goal that includes aggregated
experience from this module.

This file now contains only the minimal helpers needed to feed experience data
to that persona. Do not build custom LLM loops here.

Recommended:
- Use `superpowers:subagent-driven-development` to run the learner as a proper meta-task.
- Use `superpowers:systematic-debugging` + `superpowers:verification-before-completion`
  when debugging or validating synthesis results.
- The persona itself (`personas/fleet-learner.md`) should be maintained via
  `superpowers:writing-skills` process.

If you need raw experience data for a prompt, use the functions below.
"""

from __future__ import annotations

from agent_fleet.learning.experience import (
    aggregate_fleet_experience,
    get_fleet_experience_summary,
)


def get_synthesis_context(
    persona: str,
    *,
    max_rows: int = 80,
) -> dict[str, str]:
    """
    Returns ready-to-use context strings for a meta-learner prompt.
    Call this, then dispatch the fleet-learner persona with the result.
    """
    summary = get_fleet_experience_summary(persona, max_rows=max_rows)
    recent = aggregate_fleet_experience([persona], limit_per_persona=30).get(persona, [])

    samples = "\n".join(
        f"- [{r.get('_source_repo')}] {r.get('status')}: {str(r.get('goal', ''))[:80]}"
        for r in recent[:10]
    )

    return {
        "summary": summary,
        "recent_samples": samples,
        "full_experience_count": str(len(recent)),
    }
