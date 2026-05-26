## Role

Implement **PR5**: canonical thermo-nuclear skill id + extended dynamic equip conditions.

## Scope

- `agent_fleet/skills_lib.py`, `agent_fleet/pr_review/`, remove duplicate bundled skill if base-kit resolves
- `agent_fleet/orchestration/equip.py` — worktree / CI-fix dynamic skills
- `tests/test_skills_quality.py`, equip tests

## Branch

**`feature/skills-canonical`**

## Plan

PR5 section in buildout plan doc.

## Done when

- Single thermo-nuclear resolution path
- Extended dynamic equip tested
- Full `pytest -q` green
