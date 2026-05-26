# Skill Lifecycle Unification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make base-kit skills consistently available across every fleet lifecycle path — dispatcher `simple`/`code_review`, full pipeline (PLAN→IMPLEMENT→VERIFY→REVIEW), issue dispatch, PR-loop fix rounds, and repo-local markdown personas.

**Architecture:** `resolve_dispatch_equip` already composes execute/review skill slots + `compose_body` for the dispatcher path. Gaps: `FleetRunner`/`implementer.py` ignore equip; issue dispatch never resolves equip; repo markdown personas have no loadouts; review/fix phases only partially append review skills.

**Tech Stack:** Python 3.11+, pytest, ruff, YAML loadouts.

**Dogfood tagging:** `[FLEET]` = safely dispatchable in a worktree (see `.agent-fleet.yaml` persona_scope_allowlist). `[MANUAL]` = touches bootstrap/critical-path files — human gate or pinned-dispatch venv only.

---

## Task 1: Plan + skill access matrix doc [FLEET]

**Files:**
- Create: `docs/superpowers/plans/2026-05-25-skill-lifecycle-unification.md` (this file)
- Modify: `docs/PERSONAS.md` — add "Skill access by lifecycle" table

- [ ] Add matrix: dispatcher simple/code_review (equip ✓), full pipeline IMPLEMENT (equip ✗), issue dispatch (equip ✗), PR analyzer (thermo-nuclear ✓), PR-loop fix (equip ✗), level-up overlays (overlay only)

---

## Task 2: Integration tests for equip gaps [FLEET]

**Files:**
- Create: `tests/test_skill_lifecycle.py`

- [ ] Test: `resolve_dispatch_equip` returns compose_body with TDD skill for `coder` persona
- [ ] Test: `implement()` accepts optional `compose_body` override (stub until Task 4)
- [ ] Test: `run_pipeline` execute phase uses `task.equip.compose_body` when set (already true — regression)
- [ ] Test: document expected runner behavior — runner must attach equip before IMPLEMENT (fails until Task 5)

---

## Task 3: Loadout + repo config examples [FLEET]

**Files:**
- Modify: `examples/repo.agent-fleet.yaml` — document `skills_dir`, loadout resolution, markdown+loadout pairing
- Modify: `examples/repo-full.agent-fleet.yaml` — sample `backend.loadout.yaml` beside `backend.md`

- [ ] Show pattern: `personas/backend.md` + `personas/backend.loadout.yaml` with execute/review slots

---

## Task 4: Implementer accepts equip compose_body [FLEET]

**Files:**
- Modify: `agent_fleet/implementer.py`
- Test: extend `tests/test_skill_lifecycle.py`

- [ ] Add optional `compose_body: str | None` param to `implement()`
- [ ] When set, use compose_body instead of raw `persona.prompt_path.read_text()`
- [ ] Keep fallback to markdown stub when compose_body empty

---

## Task 5: FleetRunner resolves equip before IMPLEMENT [BOOTSTRAP]

**Files:**
- Modify: `agent_fleet/runner.py`
- Test: `tests/test_skill_lifecycle.py` or extend `tests/test_skill_lifecycle.py`

- [x] Call `resolve_dispatch_equip` once per run (after persona known, before IMPLEMENT)
- [x] Pass `equip.compose_body` into `implement()`
- [x] Attach equip to task/handoff for REVIEW phase review-skill append
- [x] Do NOT edit dispatcher.py during this task

---

## Task 6: Review phase uses equip for reviewer body [BOOTSTRAP]

**Files:**
- Modify: `agent_fleet/phases.py` (`_legacy_review_phase`, structured review if applicable)

- [x] Reviewer persona: prefer loadout compose_body when equip present
- [x] Keep `_review_skill_prompt_append` for pipeline_skills.review slots

---

## Task 7: PR-loop fix phase uses equip [BOOTSTRAP]

**Files:**
- Modify: `agent_fleet/code_review/fix.py`

- [x] Resolve equip for fix persona; inject compose_body into fix prompt

---

## Task 8: Repo-local loadout discovery [FLEET]

**Files:**
- Modify: `agent_fleet/skills_lib.py` (`load_loadout`)
- Modify: `agent_fleet/personas.py` if needed
- Test: `tests/test_loadouts.py`

- [ ] Resolve `{personas_dir}/{name}.loadout.yaml` before package default
- [ ] Fall back to package `agent_fleet/personas/{name}.loadout.yaml`

---

## Verification

```bash
cd /home/evan/Documents/agent_fleet
uv run pytest tests/test_skill_lifecycle.py tests/test_equip.py tests/test_loadouts.py -q
uv run ruff check agent_fleet/implementer.py agent_fleet/skills_lib.py tests/
```

**Dispatch order:** Tasks 1→3→2→4→8 in parallel where safe; then 5→6→7 manually or with critical_path_prefixes temporarily relaxed.
