# Agent Fleet — Setup & Operations Guide

> **Fleet config (2026-05):** All agent-fleet configuration, queue, schedules,
> watcher/systemd units, and ops scripts (`fleet_keeper`, self-heal, zombie
> sweep, worktree bootstrap) live in the
> [`agent-fleet`](https://github.com/Evan-Kim2028/agent-fleet) repo under
> `targets/silphcoanalytics.*` — **not** in this checkout. This repo keeps
> persona prompts (`agents/personas/`), scope verification (`agents/verify.py`),
> and the package pin in `agents/pyproject.toml`. PR analysis still uses
> Composer via `.github/workflows/pr-analyzer.yml`.

The Agent Fleet monitors GitHub issues for `/agent --persona <name>` commands
and dispatches a structured multi-phase pipeline (plan → research → synthesize
→ implement → verify → review → open_pr) that researches, implements, and
opens a pull request automatically. Each phase is a separate Composer
agent session with its own timeout and JSON-schema-validated response.

For a user-facing usage guide (what triggers a run, what phases do, how to
inspect / stop / configure), see `docs/agents/README.md`. This document
covers installation, the watcher service, and live troubleshooting.

## Prerequisites

- Python 3.14 (matches `agents/pyproject.toml`)
- [uv](https://docs.astral.sh/uv/) — package manager (for local dev / tests)
- `agent-fleet` CLI — install from PyPI or the git pin in `agents/pyproject.toml`
- `gh` — GitHub CLI, authenticated (`gh auth login`)
- `kimi-cli` — installed and on `$PATH`
- A `.env` file at the project root with the required API keys (see below)

## Required Environment Variables

Add these to `.env` at the project root:

```
CURSOR_API_KEY=<your Cursor API key>
REPO_FULL_NAME=<owner/repo>   # e.g. Evan-Kim2028/silphcoanalytics
```

The watcher also reads `ISSUE_NUMBER` and `COMMENT_BODY` at dispatch time — those are injected automatically by `agent-fleet-watch`.

## Configuration

Fleet behavior is controlled in the **agent-fleet** repo, not in silphcoanalytics:

| File (agent-fleet repo) | Purpose |
|-------------------------|---------|
| `targets/silphcoanalytics.agent-fleet.yaml` | Target config: dispatch, PR loop, verify, persona scope, worktree bootstrap |
| `targets/silphcoanalytics.queue.yaml` | FIFO issue dispatch queue (when enabled) |

Key sections in `targets/silphcoanalytics.agent-fleet.yaml`:

| Section | Purpose |
|---------|---------|
| `issue_dispatch.enabled` | Master switch for `/agent --persona` dispatch |
| `issue_dispatch.poll_interval_s` | Poll interval for new trigger comments |
| `pr_loop.enabled` | Automated PR review-fix-merge loop |
| `pr_loop.tiered_merge_gate` | Stricter auto-merge predicate (CI + Composer review risk + scope) |
| `persona_scope_allowlist` | Per-persona path prefixes |
| `verify.protected_paths` / `critical_path_prefixes` | Scope tripwire and self-heal paths |

Flip `issue_dispatch.enabled` to `false` in the agent-fleet target file to
fail-close dispatch globally (and optionally `pr_loop.enabled: false` to stop
the auto-merge loop). **Restart `agent-fleet-watch.service` after editing** so
the watcher reloads config — editing YAML alone does not stop an already-running
poll cycle.

### PR loop

When `pr_loop.enabled` is `true`, `agent-fleet-watch` polls open PRs on
`pr_loop.branch_prefixes`, waits for Composer PR analysis CI, may spawn a fix
round, and auto-merges on green subject to `pr_loop.tiered_merge_gate`. Merge
is blocked while a matching worktree exists or a PR carries `needs-human-review`.
See **Monitoring & Troubleshooting** below for stall diagnosis.

## Installation

### Local dev / tests (agents package)

```bash
cd agents
uv sync
```

This creates `.venv/` under `agents/` and installs the `agent-fleet` dependency
(pinned in `pyproject.toml`, currently `v0.6.3`). Entry-point scripts still
available for ops:

| Command | Purpose |
|---------|---------|
| `agent-runs` | Inspect run history from local NDJSON logs |
| `fleet-status` | Fleet status helper |

Legacy scripts `agent-dispatch` and `agent-watch` were removed — production
uses the standalone `agent-fleet-watch` binary instead.

### Production watcher (`agent-fleet-watch`)

Install the `agent-fleet` package so `agent-fleet-watch` is on `$PATH`
(e.g. `pip install "agent-fleet @ git+https://github.com/Evan-Kim2028/agent-fleet@main"`
or an editable install from a local agent-fleet checkout). Match the pin in
`agents/pyproject.toml` when rolling out.

## Running the Watcher

### Primary: `agent-fleet-watch` (systemd user service)

Install the systemd unit from the **agent-fleet** repo (not silphcoanalytics).
The unit's `WorkingDirectory` should point at this silphco checkout; config is
loaded from `targets/silphcoanalytics.*` in the agent-fleet install.

```bash
# From your agent-fleet checkout — adjust paths to match your layout
cp examples/agent-fleet-watch.service ~/.config/systemd/user/agent-fleet-watch.service

# Edit WorkingDirectory (agent-fleet checkout), EnvironmentFile (.env),
# and ensure the unit passes --workspace pointing at the agent-fleet install

systemctl --user daemon-reload
systemctl --user enable --now agent-fleet-watch.service
systemctl --user status agent-fleet-watch.service
journalctl --user -fu agent-fleet-watch.service
```

The watcher reads target YAML from the agent-fleet checkout (`targets/silphcoanalytics.*`)
plus `.env` from the silphco workspace (via the target `workspace:` path). It handles
both issue dispatch and the PR loop in one process.

### Manually (for testing)

```bash
# From the agent-fleet install; silphco is the target workspace
agent-fleet-watch --workspace /path/to/agent_fleet
```

## Fleet keeper and recovery scripts

Self-heal, zombie sweep, and worktree bootstrap previously lived in silphco
(`scripts/fleet_*`); those scripts were removed when config moved to agent-fleet.
Use the agent-fleet repo's `README.md`, `docs/QUICKSTART.md`, and
`scripts/worktree-bootstrap.sh` for current ops paths.

## Rotating the GitHub Token

The dispatcher pushes commits and merges PRs using the token returned by `gh auth token` on the watcher host. That token is the gh CLI's stored OAuth credential — there is no `GITHUB_TOKEN` env var to update. Rotate whenever:

- A token may have leaked (e.g. journald lines containing `gho_…` or `ghp_…` before the redact landed in `git_push_with_user_token`).
- Suspicious push/merge activity appears on `REPO_FULL_NAME`.
- The watcher operator changes (scheduled handoff or off-boarding).
- A periodic rotation reminder fires (every 90 days is reasonable).

Steps — run them on the watcher host as the user that owns the systemd service:

1. **Quiesce the fleet** so no run starts mid-rotation (see **Quiescing before
   restart** below). At minimum set `issue_dispatch.enabled: false` and
   `pr_loop.enabled: false` in `targets/silphcoanalytics.agent-fleet.yaml` (agent-fleet
   repo), then restart the watcher. For the full idle-drain checklist, see
   `docs/ops/fleet-watcher-rollout.md`.

2. **Revoke the old credential on GitHub.** Open https://github.com/settings/tokens (PATs) or https://github.com/settings/applications (OAuth apps, look for "GitHub CLI"). Delete the token you suspect is exposed. If unsure which is current, run `gh auth status` first — it prints the token prefix.

3. **Re-authenticate `gh`:**
   ```bash
   gh auth logout --hostname github.com
   gh auth login --hostname github.com --git-protocol https --web
   ```
   Use `--scopes "repo,workflow"` if the device-flow prompt does not request workflow scope; the dispatcher needs `repo` to push and `workflow` to update `.github/workflows/*` paths.

4. **Verify the new token works:**
   ```bash
   gh auth status                          # should report the new login
   gh auth token | head -c 4 && echo …     # confirms gh returns a fresh gho_/ghp_
   gh api user -q .login                   # round-trips a real API call
   ```

5. **Restart the watcher** to drop any cached subprocess environment:
   ```bash
   systemctl --user restart agent-fleet-watch.service
   journalctl --user -u agent-fleet-watch.service -n 50 --no-pager
   ```

6. **Re-enable dispatch:** set `issue_dispatch.enabled: true` in
   `targets/silphcoanalytics.agent-fleet.yaml`. Restart the service or wait for the next
   poll cycle.

7. **Smoke-test** by posting `/agent --persona backend` on a low-risk issue and watching the journal until the PR opens and merges. Confirm no `gho_…` / `ghp_…` substrings appear in `journalctl --user -u agent-fleet-watch.service` over the run.

If a leak was the trigger, also audit recent runs in `data/events/agent_runs/YYYY-MM-DD.ndjson` for the window between leak and revocation, and review the affected repo's push history (`gh api repos/$REPO_FULL_NAME/events --paginate`) for unexpected refs.

## Triggering an Agent Run

Post this comment on any open GitHub issue:

```
/agent --persona backend
```

Valid personas: `backend`, `frontend`, `data`, `pokemon_analyst`, `security_qa`.

Persona prompt files live in `agents/personas/<name>.md`.

The fleet will:
1. **Plan** — decide single-shot vs decompose, define scope and acceptance criteria
2. **Research** — read files the planner identified, emit structured notes
3. **Synthesize** — fuse notes into an implementation brief
4. **Implement** — write code in a git worktree under `/tmp/agent-worktrees/` on branch `agent/<persona>/<issue>-<run-id>`, commit incrementally
5. **Verify** — ruff + pytest + scope tripwire (OK / RETRY one fix attempt / FATAL)
6. **Review** — reviewer subagent returns approve / request_changes / block with structured issues
7. **TechLead** — gated escalation on high-severity findings or protected-path touches
8. **OpenPR** — push, open PR, post issue comment
9. **PR loop** (`agent-fleet-watch`) — waits for Composer PR analysis CI check, dispatches one address-review round if needed, squash-merges on green CI

A mutex label `agent-running/<persona>/<issue>` is applied by the dispatcher after it starts — it acts as a lock to prevent duplicate runs, **not** as a trigger. Only the comment itself triggers dispatch.

## Viewing Run History

Agent runs emit structured NDJSON events to `data/events/agent_runs/YYYY-MM-DD.ndjson`.

```bash
# Show last 10 runs
agent-runs

# Show phase-by-phase breakdown for last 5 runs
agent-runs --last 5 --phases

# Filter by issue number
agent-runs --issue 730 --phases

# Raw JSON output
agent-runs --last 20 --json
```

Each log record includes: `ts`, `run_id`, `issue`, `persona`, `event` (`run_start`/`phase_start`/`phase_end`/`run_end`), `phase`, `status`, `duration_s`, `detail`.

## Monitoring & Troubleshooting

The agent loop is a state machine across four observable signals. To diagnose a
stuck or misbehaving run, check them in this order — each one tells you
something different.

Durable issue-loop state for `agent-fleet-watch` lives at
`.agent-fleet-state.json` in the agent-fleet checkout (`state_root` in the
silphco target). Legacy `.agent-fleet-issue-state.json` in this workspace may
still exist from pre-centralization runs; the watcher migrates it on load.
Safe to delete for a local reset — the watcher recreates state on the next
dispatch cycle.

### 1. Watcher log

```bash
journalctl --user -fu agent-fleet-watch.service        # follow live
journalctl --user -u agent-fleet-watch.service -n 50 --no-pager   # recent snapshot
```

Single source of truth for dispatch and PR-loop decisions. Key lines include issue
trigger detection, dispatch spawn, PR-loop merge decisions, and CI failures.

If a PR is not merging, this log says why.

> **Unit name:** Use **`agent-fleet-watch.service`** in all journalctl/systemctl
> examples. Empty logs usually mean the wrong unit was queried — pre-migration
> stacks used a different systemd service name.

> **Keeper vs watcher:** Fleet keeper/self-heal logs live in the agent-fleet
> deployment (and may write to `data/state/fleet_keeper.log` in this workspace).
> Use `agent-fleet-watch.service` journald for per-dispatch and PR-loop
> decisions.

### 2. Active dispatcher worktrees

```bash
ls /tmp/agent-worktrees/
```

Names follow `<issue>-<persona>-<run-id>`. Presence means a dispatcher is
mid-run. The PR loop refuses to merge while a matching worktree exists.
Worktrees are removed when the run ends (success, failure, or crash).

A worktree that hangs around with no matching dispatch process is an orphan
and will block auto-merge indefinitely.

### 3. Dispatcher processes

```bash
pgrep -af agent-fleet
pgrep -af agents.dispatch
```

Cross-check against worktrees:

| Worktree | Process | Meaning |
|----------|---------|---------|
| present  | present | normal — run in flight |
| present  | absent  | orphan — `rm -rf` the worktree, then `git worktree prune` |
| absent   | present | mid-teardown — fine, wait |
| absent   | absent  | idle |

### 4. PR state

```bash
gh pr list --search "head:agent/" --json number,title,headRefName,state,mergedAt
gh pr checks <pr-number>
gh pr view <pr-number> --json reviews,reviewDecision
```

The externally visible outcome. Combine with signals 1–3 to localize a stall.

### Run history

For post-mortems (which phase failed, how long it took) rather than live
debugging:

```bash
agent-runs --last 10 --phases
```

### Quiescing before restart or invasive recovery

Restarting `agent-fleet-watch.service` kills in-flight dispatch runs. Before
any manual restart, watcher rollout, or token rotation, quiesce the fleet:

1. **Confirm current activity** (do not restart yet if runs are still active
   unless you accept losing them):
   - `pgrep -af agents.dispatch` → empty
   - `pgrep -af agent-fleet` → empty
   - `ls /tmp/agent-worktrees/` → empty (or only worktrees with matching live
     processes)
   - no open issue carries an `agent-running/<persona>/<issue>` label
2. Set `issue_dispatch.enabled: false` in `targets/silphcoanalytics.agent-fleet.yaml`
   (agent-fleet repo; optionally `pr_loop.enabled: false` to stop the
   auto-merge loop).
3. `systemctl --user restart agent-fleet-watch.service` so the watcher
   fail-closes new dispatches.
4. **Wait until fully idle** — repeat the step-1 checks until all clear.
5. After recovery or rollout, set `issue_dispatch.enabled: true` (and
   `pr_loop.enabled: true` if disabled) and restart again.

**Rollout** (merge to `main`, pull on deployment host, verify live build):
`docs/ops/fleet-watcher-rollout.md`. That runbook is authoritative for
operator-initiated watcher restarts after fleet code changes.

**Drain stuck:** If step 4 never clears, inspect orphan worktrees (remove only
when no matching `pgrep` process), stale `agent-running/*` labels on open
issues, and `journalctl --user -u agent-fleet-watch.service` for a hung
dispatch. Do not `rm -rf` worktrees while a matching process is still running.

## Recovering from a stuck watcher

If `agent-fleet-watch` stops making progress:

1. **Check for checkout drift:** confirm the silphco workspace on the watcher
   host matches `origin/main` on fleet-critical paths (from target
   `critical_path_prefixes`).
2. Check `journalctl --user -u agent-fleet-watch.service` for errors.
3. Quiesce before restart (see **Quiescing before restart** above).
4. Confirm `targets/silphcoanalytics.agent-fleet.yaml` parses and keys are readable
   (`issue_dispatch.enabled`, `pr_loop.enabled`, `pr_loop.tiered_merge_gate`).
5. Pull the silphco repo forward if application code changed:
   `git fetch && git checkout main && git pull`.
6. Bump/reinstall **agent-fleet** to match `agents/pyproject.toml`, then restart
   only when quiesced: `systemctl --user restart agent-fleet-watch.service`.
7. Verify recovery:
   ```bash
   systemctl --user status agent-fleet-watch.service
   journalctl --user -u agent-fleet-watch.service -n 50 --no-pager
   ```
8. Re-enable dispatch: set `issue_dispatch.enabled: true` (and
   `pr_loop.enabled: true` if disabled), then restart or wait for the next
   poll.

### Stuck PRs that never auto-merge

When a fleet PR is green and mergeable but never auto-merged, check
`journalctl --user -u agent-fleet-watch.service` for orphan worktrees, review
blocks, or CI failures. Merge manually with `gh pr merge` if the PR loop gave up
after `max_ci_fix_attempts`. PRs touching protected paths require human review.

## Development

Run the test suite from inside the `agents/` directory:

```bash
uv run pytest -q
```

Tests are in `agents/tests/` and `agents/silphco/tests/`. The slow-marked tests require a real `KIMI_API_KEY` and are skipped by default.
