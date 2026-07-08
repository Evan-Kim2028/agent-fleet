# Fleet configuration

Where global fleet settings live, how `personas_dir` resolves, and how to avoid import shadowing.

## Global config file

| Location | Status |
|----------|--------|
| `~/.agent-fleet/fleet.yaml` | **Canonical** global fleet config |

Copy the template:

```bash
mkdir -p ~/.agent-fleet
cp fleet.example.yaml ~/.agent-fleet/fleet.yaml
```

Override path for a single command:

```bash
fleet run "..." --config /path/to/fleet.yaml
```

Environment variables:

| Variable | Purpose |
|----------|---------|
| `AGENT_FLEET_CONFIG` | Global fleet.yaml path (issue dispatch watcher) |
| `CODING_FLEET_CONFIG` | Global fleet.yaml path (gateway plugin tools) |
| `AGENT_FLEET_BACKEND` | **Global** override for `default_backend` — applies to **all** entry points (`fleet run`, pr-analyzer, issue dispatch, pr_loop) via `load_fleet_config()` |
| `AGENT_FLEET_MODEL` | **Global** override for `default_model` — same scope as `AGENT_FLEET_BACKEND` |

Repo-level settings (verify commands, scope, PR loop) always come from `.agent-fleet.yaml` in the target git repo — not from the global file.

## Switching backends (one line)

Resolution order for backend and model (first non-empty wins):

1. CLI flags: `fleet run --backend grok --model grok-4.5`
2. Env: `export AGENT_FLEET_BACKEND=grok` / `export AGENT_FLEET_MODEL=grok-4.5`
3. Global config: `default_backend` / `default_model` in `~/.agent-fleet/fleet.yaml`
4. Built-in default: `cursor` (model left unset so each backend supplies its default)

One-line switches:

```bash
# Permanent (writes ~/.agent-fleet/fleet.yaml)
fleet config set-backend grok
# or: fleet config set-backend grok --model grok-4.5

# Session / CI (all entry points)
export AGENT_FLEET_BACKEND=grok
export AGENT_FLEET_MODEL=grok-4.5

# Single command
fleet run "..." --backend grok
fleet doctor --backend grok   # prints active backend + model, checks Grok auth
```

See also: [GROK.md](GROK.md), [KIMI.md](KIMI.md), [OPENROUTER.md](OPENROUTER.md), [NEW-REPO.md](NEW-REPO.md), [PERSONAS.md](PERSONAS.md), [AGENT-FLEET-DEV.md](AGENT-FLEET-DEV.md).

## default_loadout_size

Sets the fleet-wide default skill loadout size when a `complexity_tiers` entry does not explicitly set `loadout_size` for that tier.

```yaml
# ~/.agent-fleet/fleet.yaml
default_loadout_size: standard   # one of: minimal | standard
```

| Value | Meaning |
|-------|---------|
| `minimal` | Smaller base skill set per dispatch |
| `standard` | Fuller execute loadout (default when derived) |

Precedence: a per-tier `complexity_tiers` override wins over `default_loadout_size`, and the CLI `--loadout {minimal,standard}` flag wins over both for a single run (see [PERSONAS.md — Per-task skill loadouts](PERSONAS.md#per-task-skill-loadouts)).

## personas_dir

Bundled personas ship inside the installed package at `agent_fleet/personas/`. **Leave `personas_dir` unset in global `fleet.yaml`** unless you maintain a separate persona tree with an **absolute** path.

| Config file | `personas_dir` behavior |
|-------------|-------------------------|
| Global `~/.agent-fleet/fleet.yaml` | Omit → bundled package personas. If set, must be absolute; relative paths resolve against the config directory (usually wrong). |
| Repo `.agent-fleet.yaml` | Optional. Relative paths resolve against the **repo root**. Overrides bundled personas for dispatches in that repo. |

Example — repo-local personas (recommended pattern):

```yaml
# .agent-fleet.yaml
personas_dir: agents/personas
persona_scope_allowlist:
  cleanup-config:
    - fleet.example.yaml
```

Example — global override (only when you have a dedicated persona directory):

```yaml
# ~/.agent-fleet/fleet.yaml
personas_dir: /home/you/fleet-personas
```

**Do not** set `personas_dir: personas` or other relative paths in global config — fleet will look under `~/.agent-fleet/personas/`, which is empty, and persona resolution fails.

## Persona resolution order

When a persona prompt or loadout is requested, fleet searches in this order:

1. **Repo `personas_dir`** — from `.agent-fleet.yaml` when dispatching in a repo (relative paths resolve against the repo root).
2. **Bundled package personas** — `agent_fleet/personas/` inside the installed package (fallback when a file is missing from the repo tree).
3. **Skill-backed prompt** — if the persona spec sets `skill: …`, fleet searches configured `skill_dirs`.
4. **Absolute or tilde path** — `prompt: /path/to/foo.md` or `prompt: ~/fleet/foo.md`.

| Lookup | Order |
|--------|-------|
| Prompt markdown (`coder.md`, loadout `stub:`) | repo `personas_dir` → package `agent_fleet/personas/` |
| Loadout YAML (`*.loadout.yaml`) | repo `personas_dir` → package |
| `fleet.yaml` `personas:` entries | Same search order; use `prune_fleet_yaml_personas()` to drop entries whose prompt `.md` is missing from both dirs |

This lets repos override bundled personas (e.g. `agents/personas/reviewer.md`) while still resolving bundled defaults such as `coder.md` when only repo-specific personas exist locally.

Loadouts (`.loadout.yaml`) and stub markdown referenced by a loadout follow the same repo-then-package search. `YamlPersonaResolver.list_personas()` unions names from both directories plus any explicit `personas:` entries in `fleet.yaml`.

Example — repo-local override with package fallback:

```yaml
# .agent-fleet.yaml
personas_dir: agents/personas
```

| Persona | `agents/personas/` | Package fallback |
|---------|-------------------|------------------|
| `fleet-registry` | `fleet-registry.md` | — |
| `coder` | (missing) | `agent_fleet/personas/coder.md` |
| `reviewer` | `reviewer.md` (repo wins) | `agent_fleet/personas/reviewer.md` |

Run `pytest tests/test_persona_registry.py` to verify every persona under `agents/personas` and every `fleet.yaml` entry tied to that directory resolves on your checkout.

## Import shadow

Python imports the first `agent_fleet` package it finds on `sys.path`. A checkout at **`~/Documents/agent_fleet`** (underscore) is a frequent mistake: running Python with that directory as cwd or on `PYTHONPATH` loads the wrong tree — stale code, missing CLI commands, or silent behavior drift.

**Safe clone paths:** `~/agent-fleet-dev`, `~/Documents/agent-fleet` (hyphen), or any path **not** named `agent_fleet` that sits on `sys.path` as a package root.

Check your environment:

```bash
python3 scripts/check-import-shadow.py
# stricter: also warn when ~/Documents/agent_fleet exists on disk
python3 scripts/check-import-shadow.py --strict-disk
```

The script reports the active import path, flags shadow entries on `sys.path`, and **never deletes** user directories. Fix by renaming/moving the checkout, using `pip install -e .` from your dev tree, and keeping `~/Documents/agent_fleet` off `PYTHONPATH`.

## Runs and level-up storage

| Path | Purpose |
|------|---------|
| `~/.agent-fleet/fleet/runs/` | JSONL dispatch logs |
| `~/.agent-fleet/level_up/` | Persona learning journals and overlays |
| `~/.agent-fleet/skills/` | Optional user skill overrides |

Use `fleet runs` to list recorded runs, or `fleet watch` to tail a live run by id.
