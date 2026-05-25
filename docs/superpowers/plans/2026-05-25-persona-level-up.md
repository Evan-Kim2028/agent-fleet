# Persona Level-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship v1 persona loadouts, local level-up storage/journaling, orchestration equip, dispatcher experience recording, and deslop on review phase.

**Architecture:** Base-kit skill catalog ships in package; orchestration `resolve_dispatch_equip()` picks catalog skills + fleet/repo overlays; dispatcher records experience/journal; compose prompt in personas. See `docs/PERSONA-EVOLUTION.md`.

**Tech Stack:** Python 3.14, pytest, pyyaml, existing FleetLogger/RunLog.

**Worktree:** `/home/evan/Documents/agent-fleet/.worktrees/feature-persona-level-up` on branch `feature/persona-level-up`.

---

## Parallel task map

| Task | Owner | Files | Depends on |
|------|-------|-------|------------|
| **T1** | level_up core | `agent_fleet/level_up/*`, tests | — |
| **T2** | loadouts + compose | `skills_lib`, `personas`, `base-kit`, loadouts | T1 paths |
| **T3** | orchestration equip | `orchestration/equip.py`, decompose hook | T1, T2 |
| **T4** | dispatcher + phases | `dispatcher*`, `phases`, `hooks` | T1, T3 |
| **T5** | repo config + CLI | `repo.py`, `cli.py`, `config.py` | T1 |
| **T6** | integration tests | `tests/test_level_up*.py` | T1–T5 |

---

### Task T1: level_up package [FLEET]

**Files:**
- Create: `agent_fleet/level_up/__init__.py`
- Create: `agent_fleet/level_up/paths.py`
- Create: `agent_fleet/level_up/models.py`
- Create: `agent_fleet/level_up/journal.py`
- Create: `agent_fleet/level_up/experience.py`
- Create: `agent_fleet/level_up/overlay.py`
- Create: `agent_fleet/level_up/config.py`
- Test: `tests/test_level_up_core.py`

Implement:
- `LEVEL_UP_ROOT = Path.home() / ".agent-fleet" / "level_up"`
- `repo_key(name, repo_root)`, persona dirs for repo + `_fleet`
- `LevelUpConfig` from repo yaml (`train`, `contribute_to_fleet`, `journal_task_summaries`)
- `append_journal(event, repo_key, persona, run_id=None, data={})`
- `append_experience(...)` with `source`, `weight`, `pr_loop_round` fields
- `load_overlay(repo_key, persona)` → rules list + generation from meta.json
- `compose_overlay_text(rules)` for prompt injection
- Constants: `COMPACTION_IDLE_DAYS = 7`, weight constants for PR loop

Tests: journal append creates file, experience append, overlay load empty, repo_key resolution.

---

### Task T2: Loadouts + persona compose [FLEET]

**Files:**
- Create: `agent_fleet/base-kit/manifest.yaml`
- Create: `agent_fleet/base-kit/cursor-team-kit/deslop/SKILL.md` (copy from cursor plugins deslop)
- Create: `agent_fleet/personas/coder.loadout.yaml`
- Create: `agent_fleet/personas/reviewer.loadout.yaml`
- Modify: `agent_fleet/skills_lib.py`
- Modify: `agent_fleet/personas.py`
- Modify: `agent_fleet/hooks.py` (Persona: add `body`, `skill_slots`, `review_skill_slots`, `level_up_generation`)
- Test: `tests/test_loadouts.py`

Implement:
- `base_kit_dirs()`, `resolve_skill_id(skill_id)` — skill_id like `cursor-team-kit/deslop` maps to `base-kit/cursor-team-kit/deslop/SKILL.md`
- `load_loadout(name)` from `personas/*.loadout.yaml`
- `compose_persona_body(loadout, fleet_overlay, repo_overlay, extra_skills)`
- `YamlPersonaResolver.load()` uses compose with level_up overlays from T1
- `read_persona_body(persona)` helper

Default coder loadout: superpowers paths as placeholders in yaml until sync; include pstack/tdd references as ids in manifest; review loadout includes deslop in `pipeline_skills.code_review.review`.

---

### Task T3: Orchestration equip [FLEET]

**Files:**
- Create: `agent_fleet/orchestration/equip.py`
- Modify: `agent_fleet/orchestration/__init__.py`
- Modify: `agent_fleet/dispatcher.py` (call equip before pipeline)
- Modify: `agent_fleet/orchestration/decompose.py` (child tasks get parent_run_id + per-child equip log)
- Test: `tests/test_equip.py`

Implement:
- `@dataclass DispatchEquip: skill_slots_execute, skill_slots_review, level_up_generation, parent_run_id`
- `resolve_dispatch_equip(task, fleet_config, repo, run_id)` — load loadout, merge dynamic skills from recent experience (stub: add systematic-debugging if last status verify_failed in experience tail)
- Journal `equip.loadout`, `equip.compose` via level_up.journal
- Attach equip to task via new optional field on FleetTask: `equip: DispatchEquip | None` (frozen dataclass — use replace on FleetTask)

---

### Task T4: Dispatcher + review deslop [FLEET]

**Files:**
- Modify: `agent_fleet/hooks.py` (FleetTask.equip)
- Modify: `agent_fleet/dispatcher_task.py` (record experience + journal on complete)
- Modify: `agent_fleet/phases.py` (review phase load review_skill_slots + deslop text)
- Modify: `agent_fleet/config.py` (default runs dir `~/.agent-fleet/fleet/runs`)
- Test: extend `tests/test_equip.py`, `tests/test_phases_deslop.py`

Implement:
- `build_task_result` calls `experience.append` + journal `run.complete` with equip_snapshot
- Review phase: if `task.equip.review_skill_slots`, append skill bodies to reviewer prompt; emit journal event on run JSONL if fleet_log available
- Weight: pr_loop sources get weight 2.0 when round>=2 (pass source in task context or detect from phases)

---

### Task T5: Repo config + CLI [FLEET]

**Files:**
- Modify: `agent_fleet/repo.py` (LevelUpConfig on RepoConfig)
- Modify: `agent_fleet/cli.py` (subcommands: `level-up status`, `level-up journal`)
- Modify: `examples/repo.agent-fleet.yaml` (document level_up block)
- Modify: `docs/PERSONAS.md` (brief pointer to PERSONA-EVOLUTION.md)

Implement minimal CLI reading journal tail and meta.json generation.

---

### Task T6: Integration tests [FLEET]

**Files:**
- Create: `tests/test_level_up_integration.py`

End-to-end: resolve equip → compose persona body includes overlay → experience append on mock dispatch result (use tmp_path monkeypatch LEVEL_UP_ROOT).

---

## Verification

```bash
cd /home/evan/Documents/agent-fleet/.worktrees/feature-persona-level-up
uv run pytest tests -q
uv run ruff check agent_fleet tests
```

Expected: all existing 190 tests + new tests pass.

---

## Out of scope v1

~~- Full superpowers/pstack vendoring (manifest + deslop only)~~ **Done** — `scripts/sync-base-kit.sh`, 46 vendored skills
~~- `level-up train` LLM gate (stub CLI message)~~ **Done** — train/gate/approve/compact/overlap CLI
~~- Tech lead skill promotion extension~~ **Done** — `skill_promotion_review()` in `tech_lead.py`
~~- Compaction job execution (constants + journal event stub OK)~~ **Done** — `compact_persona()`, equip touch tracking
