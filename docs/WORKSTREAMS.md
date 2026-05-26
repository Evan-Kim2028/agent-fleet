# Workstreams

Workstreams are **first-class repo-defined task batches** in agent-fleet. Declare them in `.agent-fleet.yaml` and dispatch with the CLI — no ad-hoc Python scripts required.

## Configuration

```yaml
workstreams:
  plan: docs/superpowers/plans/2026-05-25-repo-cleanup-buildout.md
  base_branch: feature/skills-pr-loop
  default_target_branch: feature/repo-cleanup
  pipeline: code_review
  sequential_stack: true   # block parallel runs when persona scopes overlap
  items:
    - id: worktree
      persona: cleanup-worktree
      goal: |
        Improve worktree retention and commit-before-teardown...
      target_branch: feature/repo-cleanup
      base_branch: feature/skills-pr-loop
```

Each item becomes one fleet task with:

- `goal` / optional per-item `context`
- `persona` (must exist under `personas_dir`)
- `base_branch` for isolated worktree checkout (falls back to workstream or repo `default_branch`)
- `target_branch` communicated in task context for the agent to commit on

## CLI

Primary entry:

```bash
# List configured workstreams
agent-fleet workstream list --workspace /path/to/repo

# Run one workstream (sequential, default)
agent-fleet workstream run worktree --workspace /path/to/repo

# Preview tasks without dispatching
agent-fleet workstream run dispatch-tooling --dry-run --workspace /path/to/repo

# Run all workstreams
agent-fleet workstream run --all --workspace /path/to/repo

# Parallel batch (blocked when scopes overlap and sequential_stack: true)
agent-fleet workstream run --all --parallel --workspace /path/to/repo

# Merge a fleet-run worktree onto a feature branch
agent-fleet workstream harvest .worktrees/fleet-runs/task-0-abc123 \
  --target feature/repo-cleanup \
  --workspace /path/to/repo

# Preview harvest plan
agent-fleet workstream harvest .worktrees/fleet-runs/task-0-abc123 \
  --target feature/repo-cleanup \
  --dry-run \
  --workspace /path/to/repo
```

Module entry (also available as a standalone module):

```bash
python -m agent_fleet.workstreams list --workspace /path/to/repo
python -m agent_fleet.workstreams harvest .worktrees/fleet-runs/task-0-abc123 \
  --target feature/repo-cleanup --dry-run
```

### Preflight

Before `run --all`, run the checklist in [DISPATCH-COOKBOOK.md](DISPATCH-COOKBOOK.md#preflight-run-before-every-batch). Minimum:

```bash
pip install -e .
agent-fleet personas validate --workspace .
agent-fleet workstream list --workspace .
agent-fleet workstream run --all --dry-run --workspace .
```

### Flags

| Command | Flag | Purpose |
|---------|------|---------|
| all | `--workspace` | Repo root (walks up for `.agent-fleet.yaml`) |
| all | `--config` | Optional `fleet.yaml` path for dispatcher |
| `run` | `--all` | Run every configured item |
| `run` | `--parallel` | Concurrent dispatch when scopes are disjoint |
| `run` | `--dry-run` | Print task payloads, do not dispatch |
| `harvest` | `--target` | Branch to merge worktree commits onto |
| `harvest` | `--base` | Start ref when creating a missing target branch |
| `harvest` | `--dry-run` | Show source SHA/branch without merging |

## Scope overlap protection

When `sequential_stack: true` and you pass `--parallel`, agent-fleet compares `persona_scope_allowlist` prefixes across personas in the batch. Overlapping paths raise an error before any agent runs — preventing the PR2/PR3 `prompts/` collision class of bugs.

## Worktree lifecycle

Dispatcher behavior (see `should_keep_task_worktree`):

| Status | Worktree kept when |
|--------|-------------------|
| `completed`, `merged` | Always |
| `review_changes_requested`, `verify_failed` | When there are changed files |
| `auto_push` enabled | Isolated worktree kept for publish |

Harvest surviving worktrees with `agent-fleet workstream harvest` (or `python -m agent_fleet.workstreams harvest`).

Harvest merges by **commit SHA** (not branch name) and aborts on conflict, leaving the repo checkout unchanged on failure.

## Python API

```python
from agent_fleet.repo import find_repo_config
from agent_fleet.workstreams import run_workstreams
from agent_fleet.workstreams.cli import load_repo_workstreams

repo = find_repo_config("/path/to/repo")
config = load_repo_workstreams(repo.repo_root)
results = run_workstreams(
    repo=repo,
    config=config,
    item_ids=["worktree", "config"],
    parallel=False,
)
```

## Related

- [DISPATCH-COOKBOOK.md](DISPATCH-COOKBOOK.md) — sequential vs parallel, harvest workflow
- [REDISPATCH.md](REDISPATCH.md) — hard-failure retries vs `code_review.auto_fix`
- [AGENT-FLEET-DEV.md](AGENT-FLEET-DEV.md) — equip and persona loadouts
