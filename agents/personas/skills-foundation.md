## Role

Implement **PR1** of the skills integration buildout: base-kit catalog sync, curated default loadouts, and dynamic equip rules.

## Scope

- `scripts/sync-base-kit.sh`, `agent_fleet/base-kit/`
- `agent_fleet/personas/coder.loadout.yaml`, `reviewer.loadout.yaml`
- `agent_fleet/skills_lib.py`, `agent_fleet/orchestration/equip.py`
- `tests/test_loadouts.py`, `tests/test_equip.py`, `tests/test_phases_deslop.py`
- `docs/AGENT-FLEET-DEV.md`, `docs/PERSONA-EVOLUTION.md`

## Branch

Work on **`feature/skills-foundation`**. Do not start PR2–PR5 work in this branch.

## Plan

Read `docs/superpowers/plans/2026-05-25-skills-integration-buildout.md` — PR1 section only.

## Done when

- `./scripts/sync-base-kit.sh` vendored; manifest updated
- Coder loadout is pstack-first (no superpowers duplication)
- Reviewer review phase: `pstack/unslop` + `cursor-team-kit/deslop`
- Dynamic equip: `pstack/why` on verify_failed; pr_loop CI skills when enabled
- `pytest tests/test_loadouts.py tests/test_equip.py tests/test_phases_deslop.py -q` passes
- Changes committed on `feature/skills-foundation`
