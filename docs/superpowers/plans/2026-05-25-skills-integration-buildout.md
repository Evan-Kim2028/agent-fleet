# Skills Integration Buildout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make agent-fleet use the base-kit skill catalog consistently across every agent dispatch path — dispatcher, PR loop, code_review auto-fix, and secondary personas — with curated defaults and situational dynamic equip.

**Architecture:** Introduce a single prompt builder (`agent_fleet/prompts/agent.py`) that layers `resolve_dispatch_equip()` → `compose_body` → task-specific sections. All `backend.run()` call sites that represent a persona doing work must go through it. Loadouts remain human-curated recipes; `equip.py` owns situational skill injection. Work lands as **5 stacked PRs** in isolated worktrees off `main`.

**Tech Stack:** Python 3.14, pytest, existing FleetTask/DispatchEquip, base-kit sync script, git worktrees under `.worktrees/`.

---

## Problem summary (from audit)

| Gap | Impact |
|-----|--------|
| PR loop (`lifecycle.py`) hand-rolls prompts | CI/review fix agents miss fix-ci, verify-this, pstack principles |
| `code_review/fix.py` hand-rolls prompts | auto-fix loop misses equip |
| Only `coder` + `reviewer` loadouts | pr-analyzer, explorer, scouts run skill-less |
| v0.7.0 coder loadout duplicated superpowers + pstack | ~30k chars redundant guidance |
| Review had deslop OR unslop, not both | code vs prose cleanup split wrong |
| Dynamic equip only on `verify_failed` | pr_loop, worktree, CI-fix contexts ignored |
| thermo-nuclear in `agent_fleet/skills/` AND base-kit | two ids, drift risk |
| Large uncommitted WIP on `main` | blocks clean branch stack |

---

## Branch / worktree stack

Work from **`main` @ v0.7.0** (or current `main` after stashing WIP). Each PR merges into the previous branch tip (stack) or rebase onto merged predecessor.

```
main
 └── feature/skills-foundation      PR1  (land WIP + sync + loadout curation)
      └── feature/skills-prompt-builder   PR2  (shared prompt builder)
           └── feature/skills-pr-loop      PR3  (wire PR loop + code_review fix)
                └── feature/skills-personas    PR4  (secondary persona loadouts)
                     └── feature/skills-canonical PR5  (thermo-nuclear + equip polish)
```

**Worktree commands** (run from repo root):

```bash
git stash push -u -m "wip skills foundation"
git worktree add .worktrees/skills-foundation -b feature/skills-foundation main
# After PR1 merges:
git worktree add .worktrees/skills-prompt-builder -b feature/skills-prompt-builder main
# … repeat per branch
```

Restore stash into `feature/skills-foundation` worktree, not `main`.

---

## PR1: `feature/skills-foundation` — catalog + curated defaults

**Goal:** Ship vendored cursor-team-kit catalog and sane default loadouts; dynamic equip for pr_loop + verify_failed.

### Files

- Modify: `scripts/sync-base-kit.sh` (full cursor-team-kit rsync — **done in WIP**)
- Modify: `agent_fleet/base-kit/manifest.yaml`, vendored trees
- Modify: `agent_fleet/personas/coder.loadout.yaml`, `reviewer.loadout.yaml`
- Modify: `agent_fleet/skills_lib.py` (`PR_LOOP_EXECUTE_SKILLS`, `SYSTEMATIC_DEBUGGING_SKILL = pstack/why`)
- Modify: `agent_fleet/orchestration/equip.py` (pr_loop dynamic skills — **done in WIP**)
- Modify: `docs/AGENT-FLEET-DEV.md`, `docs/PERSONA-EVOLUTION.md`
- Test: `tests/test_loadouts.py`, `tests/test_equip.py`

### Coder execute (final)

```yaml
skills:
  execute:
    - pstack/tdd
    - pstack/principle-prove-it-works
    - pstack/principle-fix-root-causes
    - pstack/principle-boundary-discipline
    - pstack/principle-minimize-reader-load
    - pstack/principle-never-block-on-the-human
    - pstack/principle-guard-the-context-window
    - cursor-team-kit/verify-this
    - pstack/how
    - pstack/figure-it-out
```

**Remove** all `superpowers/*` from default coder loadout (keep superpowers in catalog for custom loadouts).

### Reviewer review phase (final)

```yaml
pipeline_skills:
  code_review:
    review:
      - pstack/unslop
      - cursor-team-kit/deslop
```

### Dynamic equip rules (equip.py)

| Condition | Skills appended to execute |
|-----------|---------------------------|
| `last_experience_shows_verify_failed` | `pstack/why` |
| `repo.pr_loop.enabled` | `fix-ci`, `loop-on-ci`, `get-pr-comments` |

### Tasks

- [ ] Stash/commit-split WIP: separate unrelated changes (fleet_paths, pr_loop preflight) if needed
- [ ] Run `./scripts/sync-base-kit.sh`; commit vendored trees + manifest
- [ ] Apply loadout yaml + skills_lib + equip changes
- [ ] `pytest tests/test_loadouts.py tests/test_equip.py tests/test_phases_deslop.py -q`
- [ ] Update docs default skills section
- [ ] Open PR1

---

## PR2: `feature/skills-prompt-builder` — single prompt assembly

**Goal:** DRY prompt construction so every path can prepend persona+skills consistently.

### Files

- Create: `agent_fleet/prompts/__init__.py`
- Create: `agent_fleet/prompts/agent.py`
- Create: `tests/test_prompts_agent.py`

### API

```python
@dataclass(frozen=True)
class AgentPrompt:
    full: str
    persona_section: str
    task_section: str

def build_agent_prompt(
    *,
    persona_body: str,
    task_heading: str,
    task_body: str,
    context: str = "",
    extra_sections: list[tuple[str, str]] | None = None,
) -> AgentPrompt:
    """Layer persona (skills+stub+overlays) then structured task sections."""
```

Rules:
- `persona_body` is always `equip.compose_body` when equip exists, else `read_persona_body(persona)`
- Task sections use consistent `##` headings (matches phases.py style)
- `extra_sections` = `[("Review", review_body), ("PR changed files", ...)]`

### Refactor (minimal in PR2 — just extract + test)

- [ ] Implement `build_agent_prompt` with tests
- [ ] Refactor `phases._build_execute_prompt` to call it (behavior-preserving)
- [ ] `pytest tests/test_phases_execute_equip.py tests/test_prompts_agent.py -q`
- [ ] Open PR2

---

## PR3: `feature/skills-pr-loop` — wire equip into fix agents

**Goal:** PR loop and code_review auto-fix use the same skill stack as dispatcher.

### Files

- Modify: `agent_fleet/pr_loop/lifecycle.py` — `address_review_findings`, `attempt_ci_fix`
- Modify: `agent_fleet/code_review/fix.py` — `run_fix_phase`
- Modify: `agent_fleet/prompts/agent.py` — optional `equip_context` tag for journal
- Create: `tests/test_pr_loop_equip.py`
- Create: `tests/test_code_review_fix_equip.py`

### Pattern (both PR loop functions)

```python
from agent_fleet.hooks import FleetTask
from agent_fleet.orchestration.equip import resolve_dispatch_equip
from agent_fleet.prompts.agent import build_agent_prompt

task = FleetTask(
    goal=f"Fix PR #{pr_number} review findings",
    context=f"branch={branch}",
    persona=fix_persona_name,
    workspace=str(worktree),
)
equip = resolve_dispatch_equip(task, fleet_config, repo, run_id=f"pr-loop-{pr_number}")
prompt = build_agent_prompt(
    persona_body=equip.compose_body,
    task_heading="Task",
    task_body=...,  # existing review/CI instructions
    extra_sections=[("Review", review_body), ...],
).full
```

Notes:
- Pass `repo` into equip so pr_loop dynamic skills apply
- Keep `persona_obj.model/mode/allowed_tools` from resolver
- CI fix task goal should mention failed checks so verify-this/fix-ci skills activate contextually

### code_review/fix.py

- Accept optional `task.equip` or call `resolve_dispatch_equip` inside fix phase
- Prepend `equip.compose_body` to fix prompt (same as execute phase)

### Tasks

- [x] Write failing tests: PR loop prompt contains `# Fix CI` or skill marker from fix-ci
- [x] Wire lifecycle.py (review fix + CI fix)
- [x] Wire code_review/fix.py
- [x] `pytest tests/test_pr_loop_equip.py tests/test_code_review_fix_equip.py -q`
- [ ] Manual smoke: `agent-fleet loop` dry path with mock backend if available
- [ ] Open PR3

---

## PR4: `feature/skills-personas` — loadouts for all bundled personas

**Goal:** Every first-class persona gets a loadout; markdown stubs stay thin.

### New loadouts

| Persona | execute skills | review / notes |
|---------|----------------|----------------|
| `pr-analyzer` | `pstack/interrogate`, `pstack/how` | quality pass stays in `pr_review/prompts.py` (thermo-nuclear) |
| `explorer` | `pstack/how`, `pstack/why` | read-only; mode plan |
| `tech-scout` | `pstack/how`, `pstack/figure-it-out` | read-only scout |
| `product-scout` | `pstack/how`, `pstack/reflect` | read-only scout |

Optional: `planner` persona if full pipeline uses one — add `pstack/architect` + `superpowers/brainstorming` only if planner is a distinct persona name in fleet.yaml.

### Files

- Create: `agent_fleet/personas/pr-analyzer.loadout.yaml`, `explorer.loadout.yaml`, etc.
- Modify: `tests/test_loadouts.py` — parametrize all loadouts resolve
- Modify: `docs/PERSONAS.md` — loadout table

### Tasks

- [x] Add loadout yamls + stub references
- [x] Test every skill id resolves under base-kit
- [ ] Open PR4

---

## PR5: `feature/skills-canonical` — dedupe + extended dynamic equip

**Goal:** One canonical thermo-nuclear id; richer situational equip without bloating every dispatch.

### Thermo-nuclear canonical id

- Prefer: `cursor-team-kit/thermo-nuclear-code-quality-review` in base-kit
- Change `DEFAULT_QUALITY_REVIEW_SKILL` and `agent_fleet/skills/` to re-export or symlink via `resolve_skill_path` search order (bundled dir → base-kit)
- Delete duplicate `agent_fleet/skills/thermo-nuclear-code-quality-review/SKILL.md` once base-kit resolves
- Update Hermes copy or point Hermes at base-kit path in deploy script

### Extended dynamic equip (equip.py)

| Condition | Skills |
|-----------|--------|
| `repo.use_worktree` or task on worktree branch | `superpowers/using-git-worktrees` (if not in loadout) |
| task.context contains `ci_fix` or phase is CI fix | `cursor-team-kit/fix-ci` (even without pr_loop) |
| task.pipeline == `full` && persona == planner | `pstack/architect` (if planner loadout exists) |

Keep guards: skip if already in loadout; skip if `skill_exists_in_base_kit` false.

### Tasks

- [ ] Unify thermo-nuclear resolution + tests in `test_skills_quality.py`
- [ ] Add dynamic equip conditions + tests
- [ ] Run full `pytest -q`
- [ ] Open PR5

---

## Out of scope (v1) — track as follow-ups

- **`poteto-mode` as coder loadout replacement** — needs A/B; defer
- **Full pipeline phases** (planner/researcher/synthesizer/implementer) — audit after PR3 pattern proven; many already use sessions via runner
- **Level-up promoting catalog skill ids** — overlay text only today; separate feature
- **Hermes skill duplication** — trim in deploy script after PR5

---

## Verification gate (every PR)

```bash
cd /path/to/worktree
uv sync --frozen --group dev
pytest -q
ruff check agent_fleet tests
ty check agent_fleet  # if configured in CI
```

PR merges only when CI green. Stack rebase: each new branch rebases onto merged predecessor before merge to main.

---

## Execution order for agents

1. **PR1** — unblocks catalog; land first (includes current WIP)
2. **PR2** — required before PR3 (shared builder)
3. **PR3** — highest user-visible fix (PR loop)
4. **PR4** — parallelizable after PR1 if PR2/3 in flight
5. **PR5** — cleanup after PR1–4 merged

Estimated: **PR1 ~2h, PR2 ~1h, PR3 ~3h, PR4 ~1.5h, PR5 ~2h** (agent time, parallelizable PR4).

---

## Success criteria

- [ ] PR loop fix prompts include composed skill text (grep test for skill headings)
- [ ] Coder loadout has no superpowers/pstack duplication
- [ ] Reviewer review phase runs unslop + deslop
- [x] All bundled personas have loadouts; all ids resolve
- [ ] thermo-nuclear single canonical path
- [ ] `pytest -q` green on main after full stack merges
