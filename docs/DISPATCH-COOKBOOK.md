# Dispatch Cookbook

How to run agent-fleet on itself without losing work or colliding parallel agents.

## Prefer workstreams over scripts

Declare batches in `.agent-fleet.yaml` under `workstreams:` and run:

```bash
agent-fleet workstream run worktree
agent-fleet workstream run --all
```

Until `agent-fleet workstream` is wired into the top-level CLI, use the module entry:

```bash
python -m agent_fleet.workstreams run --all --workspace .
python -m agent_fleet.workstreams list
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

## Integration checklist

Wire workstreams into the installed CLI (outside this module):

1. `agent_fleet/cli.py` — call `register_workstream_commands(sub)`
2. `agent_fleet/repo.py` — expose `RepoConfig.workstreams` via `load_workstreams_config`
3. `agent_fleet/__init__.py` — export `run_workstreams`

The module is self-contained until those hooks land; `python -m agent_fleet.workstreams` exercises list/run/harvest end-to-end.
