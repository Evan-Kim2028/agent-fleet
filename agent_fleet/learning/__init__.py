"""Self-improving flywheel for Agent Fleet.

This module contains the logic for the fleet to analyze its own experience
across repositories and update global (_fleet) skills.

The goal is a closed loop:
  Runs (across repos) → Experience in ~/.agent-fleet/
  → Synthesis → Skill promotion → Better future runs
"""

from agent_fleet.learning.experience import (
    aggregate_fleet_experience,
    get_fleet_experience_summary,
)
from agent_fleet.learning.llm_synthesis import propose_skills_with_llm
from agent_fleet.learning.synthesizer import (
    synthesize_fleet_skills,
    trigger_fleet_learning_cycle,
)

__all__ = [
    "aggregate_fleet_experience",
    "get_fleet_experience_summary",
    "propose_skills_with_llm",
    "synthesize_fleet_skills",
    "trigger_fleet_learning_cycle",
]
