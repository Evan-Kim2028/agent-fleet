"""Self-improving flywheel integration for Agent Fleet.

**LESS IS MORE:** The real power comes from the vendored Cursor superpowers
skills + the existing level_up system, not from custom code here.

See:
- superpowers:verification-before-completion (use this before any "flywheel works" claim)
- superpowers:systematic-debugging
- superpowers:writing-skills (for the fleet-learner persona)
- superpowers:subagent-driven-development (preferred pattern for the meta loop)

This package is a thin adapter layer only.
"""

from agent_fleet.learning.experience import (
    aggregate_fleet_experience,
    get_fleet_experience_summary,
)
from agent_fleet.learning.synthesizer import (
    synthesize_fleet_skills,
    trigger_fleet_learning_cycle,
)

__all__ = [
    "aggregate_fleet_experience",
    "get_fleet_experience_summary",
    "synthesize_fleet_skills",
    "trigger_fleet_learning_cycle",
]
