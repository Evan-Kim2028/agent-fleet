# Persona evolution, loadouts, and level-up

Architecture for skill-backed personas, local journaling, and orchestration-owned equip + promotion.

**Reference plan:** `docs/superpowers/plans/2026-05-25-persona-level-up.md`

---

## Principles

1. **Skills ≠ memories** — Experience is raw input; only gated rules become overlay skills (technical, output-related, playbook-shaped).
2. **Base kit is human-only** — Ships with the package; fleet never writes under `agent_fleet/base-kit/`.
3. **Orchestration owns cross-run decisions** — Plan, equip (`skill_slots`), level-up train, gate, compaction, fleet promotion.
4. **Dispatcher owns per-run execution** — Pipelines, experience append, run JSONL; no overlay promotion.
5. **Tech lead** — Existing high-risk ship gate on `full` pipeline; extended for hard skill promotions (`domain_*`, `_fleet`).
6. **Local only** — All evolution under `~/.agent-fleet/` on each machine.

---

## Directory layout

### Package (human)

```
agent_fleet/base-kit/
  manifest.yaml                 # pinned upstream SHAs, licenses
  superpowers/                  # vendored skills (sync script)
  pstack/                       # vendored skills
  cursor-team-kit/deslop/

agent_fleet/personas/
  *.loadout.yaml                # human recipes → base-kit skill ids
  coder.md                      # thin fleet stubs (optional)
```

### Local (fleet)

```
~/.agent-fleet/
  fleet/runs/<run-id>.jsonl     # per-dispatch timeline
  journal/index.jsonl           # optional global run index
  level_up/
    _fleet/<persona>/
      overlay.yaml              # cross-repo skills
      journal.jsonl
      experience.jsonl
      meta.json
      candidates/
      retired/
    <repo-key>/<persona>/
      (same structure)
```

**Compose order at dispatch:** base-kit loadout → `_fleet` overlay → repo overlay → task context.

---

## Skill vs memory

| Memory (reject) | Skill (allow after gate) |
|-----------------|--------------------------|
| Issue numbers, branch names, one-off paths | Methodology, stack, domain patterns |
| Personal trivia | Iceberg vs Parquet gotchas, verify discipline |

Gate pipeline (orchestration, not a separate product component):

1. Deterministic filters (episodic patterns)
2. LLM skill-shape check (internal step; journal `level_up.gate.classify`)
3. Evidence (outcome_delta, repeat pattern, holdout)
4. Tech lead skill review for `domain_*` / `_fleet`
5. Promote to `overlay.yaml`

---

## Overlay rule schema

```yaml
schema_version: 1
rules:
  - id: verify-before-done
    kind: methodology          # methodology | stack | domain_data | domain_app | review_quality
    text: "Run repo verify_commands before claiming completion."
    pinned: false
    stack_tags: []
    area_patterns: []
    provenance:
      - repo_key: agent-fleet
        area: agent_fleet/
        task_summary: "Fix dispatcher tests"
        note: "Learned from agent-fleet (agent_fleet/) — verify_failed"
    confidence: 0.8
```

---

## Equip (orchestration)

`resolve_dispatch_equip(task, repo, history)` returns:

```yaml
persona: coder
pipeline: code_review
base_loadout: coder
skill_slots_execute: [...]      # catalog ids from base-kit
skill_slots_review: [cursor-team-kit/deslop]
level_up_generation: 2
parent_run_id: null             # set for decomposed children
source_weight: 1.0
```

Dynamic `skill_slots` add catalog skills from base-kit only (never freeform journal text).

---

## Defaults (locked)

| Knob | Value |
|------|--------|
| Max active rules | Unlimited (v1) |
| Fleet `min_repos` | 1 (no fixed K gate; quality + evidence) |
| Compaction idle | **7 days** without equip |
| Auto-promote without tech lead | **`methodology` + `stack` only** |
| PR-loop weight | **2.0** when `pr_loop_round >= 2`; **1.5** review-fix→success; **1.0** default |

---

## Privacy (`.agent-fleet.yaml`)

```yaml
level_up:
  train: true
  contribute_to_fleet: true
  journal_task_summaries: true
```

- `train: false` — no new experience/train for this repo
- `contribute_to_fleet: false` — local overlay OK; never promote to `_fleet`
- `journal_task_summaries: false` — provenance area-only notes

---

## Journaling

Every equip/promote/compact emits structured events to:

- `~/.agent-fleet/fleet/runs/<run-id>.jsonl` (when `run_id` set)
- `~/.agent-fleet/level_up/<repo>/<persona>/journal.jsonl`

Event namespaces: `equip.*`, `phase.review.deslop`, `run.complete`, `experience.appended`, `level_up.gate.*`, `level_up.compact.*`.

---

## Pipelines

- **Execute** — persona loadout + overlays; no deslop
- **Review** — reviewer loadout + **deslop** (`review_skill_slots`)

PR loop, issue dispatch, and CLI all go orchestration equip → dispatcher.

---

## Compaction

Retire rules (to `retired/`) when:

- Not equipped for **7 days** (unless `pinned: true`)
- Low outcome rate after ≥5 equips
- Superseded by newer rule (merge provenance)

Journal: `level_up.compact.retired`.

---

## CLI

```bash
agent-fleet level-up status --repo NAME --persona coder
agent-fleet level-up journal --repo NAME --persona coder --tail 50
agent-fleet level-up overlap --repo NAME --persona coder
agent-fleet level-up train --persona coder --repo NAME [--dry-run]
agent-fleet level-up approve --repo NAME --persona coder --candidate ID
agent-fleet level-up compact --repo NAME --persona coder
```

---

## Components

| Module | Role |
|--------|------|
| `agent_fleet/level_up/` | paths, journal, experience, overlay, equip, gate, compaction |
| `agent_fleet/orchestration/equip.py` | `resolve_dispatch_equip`, child equip |
| `agent_fleet/personas.py` | loadout + compose prompt |
| `agent_fleet/dispatcher.py` | pass equip context |
| `agent_fleet/dispatcher_task.py` | record experience + journal |
| `agent_fleet/phases.py` | review deslop skills |
| `agent_fleet/tech_lead.py` | skill promotion review (extension) |

No separate “trainer” or “classifier” product terms.
