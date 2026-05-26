## Role

Implement **PR2**: shared `build_agent_prompt()` and refactor execute prompt assembly.

## Scope

- Create `agent_fleet/prompts/agent.py`, `tests/test_prompts_agent.py`
- Refactor `agent_fleet/phases.py` `_build_execute_prompt` to use it (behavior-preserving)

## Branch

**`feature/skills-prompt-builder`** — branch from merged PR1 or rebase onto `feature/skills-foundation` if PR1 not merged yet.

## Plan

`docs/superpowers/plans/2026-05-25-skills-integration-buildout.md` — PR2 section.

## Done when

- Tests pass; no behavior change to equip compose order
- Committed on `feature/skills-prompt-builder`
