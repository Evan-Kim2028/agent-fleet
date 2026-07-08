# Dispatch Cookbook

How to run agent-fleet on itself without losing work or colliding parallel agents.

## Preflight (run before every batch)

Skips the common failure modes that waste 3–6 minutes per workstream:

```bash
# Installed CLI matches repo (workstream subcommand, scope fixes)
pip install -e . --quiet

# Personas resolve (repo dir → package fallback)
agent-fleet personas validate --workspace .

# Workstreams and scopes are sane
agent-fleet workstream list --workspace .
python -m agent_fleet.workstreams run --all --dry-run --workspace .

# Optional: full test baseline
pytest -q
```

Checklist:

| Check | Why |
|-------|-----|
| `pip install -e .` | Stale `agent-fleet` binary missing `workstream` |
| `personas validate` | Missing `coder.md` / loadouts under `personas_dir` |
| `workstream list` + dry-run | Overlapping scopes, bad persona names |
| Allowlist matches goal | e.g. registry needs `tests/`, `docs/`, `config.py` |
| Harvest completed worktrees first | Avoid duplicate execute passes |

## Prefer workstreams over scripts

Declare batches in `.agent-fleet.yaml` under `workstreams:` and run:

```bash
agent-fleet workstream run worktree
agent-fleet workstream run --all
```

See [WORKSTREAMS.md](WORKSTREAMS.md).

## Sequential vs parallel

| Mode | When |
|------|------|
| Sequential (`run --all`) | Stacked PRs, shared modules, default |
| Parallel (`run --all --parallel`) | Disjoint `persona_scope_allowlist` only |

Parallel PR2 + PR3 both touching `agent_fleet/prompts/` is the canonical failure — blocked when `sequential_stack: true`.

Preview a batch before spending agent time:

```bash
python -m agent_fleet.workstreams run dispatch-tooling --dry-run --workspace .
```

## Worktree lifecycle

1. Fleet creates `.worktrees/fleet-runs/task-N-*` with `base_branch` from task or repo config.
2. On `completed`, worktree is kept.
3. On `review_changes_requested` with changes, worktree is kept (`should_keep_task_worktree`).
4. Preview harvest: `python -m agent_fleet.workstreams harvest <path> --target feature/my-branch --dry-run`
5. Harvest: `python -m agent_fleet.workstreams harvest <path> --target feature/my-branch`

Harvest merges the worktree HEAD commit onto the target branch by SHA. Use `--base` when the target branch does not exist yet.

## auto_fix vs redispatch

- **`code_review.auto_fix`**: same task, fix persona, re-run review (soft failure).
- **`max_redispatches`**: new session on hard failures (`error`, `timeout`, `scope_violation`).

Enable `auto_fix` in `.agent-fleet.yaml` for self-dispatch buildouts.

## Skills buildout (legacy script)

`scripts/dispatch-skills-buildout.py` remains for the 5-PR stack; new cleanup work uses workstreams only.

To size a single dispatch's skill set instead of loading the full persona loadout, use the per-task `fleet run` flags `--skills`, `--add-skills`, and `--loadout {minimal,standard}`. See [PERSONAS.md — Per-task skill loadouts](PERSONAS.md#per-task-skill-loadouts).

## Integration checklist

For self-hosted batches on agent-fleet:

1. `.agent-fleet.yaml` — `workstreams`, `persona_scope_allowlist`, `capacity.max_dispatches`
2. `agents/personas/` — fleet dispatch personas referenced by workstream items
3. Preflight (above) — then `agent-fleet workstream run --all`
4. Harvest surviving worktrees onto `default_target_branch`
