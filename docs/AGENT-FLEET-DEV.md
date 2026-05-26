# agent-fleet-dev — fresh install walkthrough

Install agent-fleet from scratch on a new machine or side-by-side with production. Uses **`~/agent-fleet-dev`** as the git checkout name.

Hermes is **optional** (Phase 7). Fleet storage lives under **`~/.agent-fleet/`** only.

---

## Phase 0 — Prerequisites

| Requirement | Check |
|-------------|--------|
| Python **3.14** | `python3 --version` |
| **git** + **gh** | `gh auth status` |
| **uv** (recommended) | `uv --version` |
| **Cursor API key** | [dashboard/integrations](https://cursor.com/dashboard/integrations) |

```bash
export CURSOR_API_KEY="..."
```

Or copy from an existing fleet `.env` (same key the silphco watcher uses):

```bash
grep '^CURSOR_API_KEY=' /path/to/existing/.env > ~/agent-fleet-dev/.env
chmod 600 ~/agent-fleet-dev/.env
```

Load before runs:

```bash
set -a && source ~/agent-fleet-dev/.env && set +a
```

---

## Phase 1 — Clone and install

```bash
git clone https://github.com/Evan-Kim2028/agent-fleet.git ~/agent-fleet-dev
cd ~/agent-fleet-dev
uv sync --frozen --group dev
```

Verify the CLI points at this checkout:

```bash
uv run agent-fleet paths
python3 -c "import agent_fleet; print(agent_fleet.__version__, agent_fleet.__file__)"
uv run agent-fleet personas
```

---

## Phase 2 — Global config (`~/.agent-fleet/`)

```bash
mkdir -p ~/.agent-fleet
cp ~/agent-fleet-dev/fleet.example.yaml ~/.agent-fleet/fleet.yaml
```

Minimal edits:

```yaml
default_backend: cursor
default_model: composer-2.5
default_persona: coder
max_parallel: 5
```

```bash
uv run --directory ~/agent-fleet-dev agent-fleet paths
python3 ~/agent-fleet-dev/scripts/check-import-shadow.py
```

Upgrading an old machine with Hermes-hosted config:

```bash
uv run --directory ~/agent-fleet-dev agent-fleet migrate-home
```

---

## Phase 3 — Smoke test

```bash
set -a && source ~/agent-fleet-dev/.env && set +a

export REPO=/home/evan/Documents/lake-of-rage

uv run --directory ~/agent-fleet-dev agent-fleet run \
  "Add a trivial smoke test under packages/lakestore/tests/" \
  --workspace "$REPO" \
  --persona lakestore \
  --pipeline simple
```

Check:

- JSON result with `phases`
- Log: `~/.agent-fleet/runs/<run-id>.jsonl`

---

## Phase 4 — Repo factory

```bash
uv run --directory ~/agent-fleet-dev agent-fleet init "$REPO"
# edit $REPO/.agent-fleet.yaml — scope, verify_commands, level_up
```

---

## Phase 5 — Background watcher (optional)

```ini
# ~/.config/systemd/user/agent-fleet-watch.service
ExecStart=/home/you/.local/bin/uv run --directory /home/you/agent-fleet-dev agent-fleet-watch --workspace %REPO%
EnvironmentFile=%REPO%/.env
```

---

## Phase 6 — Hermes (optional interface)

```bash
cd ~/agent-fleet-dev && ./scripts/deploy-hermes.sh
```

Hermes gets the **cursor-fleet** plugin only. Config stays in `~/.agent-fleet/fleet.yaml`.

---

## Defaults reference

### Storage (`agent-fleet paths`)

| Path | Purpose |
|------|---------|
| `~/.agent-fleet/fleet.yaml` | Global personas, models, pipelines |
| `~/.agent-fleet/runs/` | Per-dispatch JSONL logs |
| `~/.agent-fleet/level_up/` | Persona learning (journal, experience, overlays) |
| `~/.agent-fleet/skills/` | Optional user skill overrides |

### Bundled personas (`fleet.example.yaml`)

Leave `personas_dir` unset in global `~/.agent-fleet/fleet.yaml` — bundled personas load from the installed package. For repo-local personas, set `personas_dir` in `.agent-fleet.yaml` (relative to repo root). Details: [FLEET-CONFIG.md](FLEET-CONFIG.md).

| Persona | Role |
|---------|------|
| `coder` | Implementer (default) |
| `reviewer` | Diff review |
| `pr-analyzer` | Two-pass PR analysis |
| `explorer` | Read-only exploration |
| `product-scout` / `tech-scout` | Scout pipeline |

### Pipelines

| Pipeline | Phases |
|----------|--------|
| `simple` | execute |
| `code_review` | execute → review (optional auto-fix loop via `code_review.auto_fix`) |
| `full` | plan → research → synthesize → implement → verify → review |

### Equip + prompt path

Persona dispatches resolve skills via `resolve_dispatch_equip()` (dispatcher) and assemble
prompts with `build_agent_prompt()` (`agent_fleet/prompts/agent.py`).

| Call site | Equip | `build_agent_prompt` |
|-----------|-------|----------------------|
| `phases.run_execute_phase` | `task.equip.compose_body` | yes |
| `phases._legacy_review_phase` | review skills via `task.equip.skill_slots_review` | yes |
| `code_review.fix.run_fix_phase` | `_resolve_fix_equip` / `task.equip` fast path | yes |
| `pr_loop.lifecycle` (review + CI fix) | `resolve_dispatch_equip` per attempt | yes |

Structured review (`reviewer.review`) injects review skills through `task_context`; planner,
implementer, researcher, and scout paths are pipeline-specific and do not use equip yet.

**`code_review.auto_fix` defaults** (repo `.agent-fleet.yaml`):

- Explicit `code_review:` block — defaults `auto_fix: false` unless set
- `pr_loop.enabled: true` with no `code_review:` section — inherits `auto_fix: true`,
  `auto_push: true`, `auto_pr_loop: true`
- See `examples/repo-full.agent-fleet.yaml` and `examples/repo.agent-fleet.yaml`

### Default skills (pstack)

Execute loadouts use **[Cursor pstack](https://github.com/cursor/plugins/tree/main/pstack)** skills vendored in `agent_fleet/base-kit/pstack/`:

**Coder:** `pstack/tdd`, verification principles, `principle-never-block-on-the-human`, `principle-guard-the-context-window`, `cursor-team-kit/verify-this`, `pstack/how`, `pstack/figure-it-out`

**Reviewer:** `pstack/interrogate`, `pstack/reflect` · review phase: `pstack/unslop`, `cursor-team-kit/deslop`

**Dynamic (on verify failure):** `pstack/why` appended to execute slots

**Dynamic (when `pr_loop.enabled`):** `cursor-team-kit/fix-ci`, `loop-on-ci`, `get-pr-comments` appended to execute slots

**Base-kit catalog:** full `pstack/`, `superpowers/`, and `cursor-team-kit/` skill trees (sync via `./scripts/sync-base-kit.sh`)

**Superpowers** remain in `base-kit/superpowers/` for optional custom loadouts; not in default loadouts.

Refresh vendored skills: `./scripts/sync-base-kit.sh`

---

## Daily dev loop

```bash
cd ~/agent-fleet-dev && git pull && uv sync --frozen --group dev
pytest -q
systemctl --user restart agent-fleet-watch.service   # if using watcher
```
