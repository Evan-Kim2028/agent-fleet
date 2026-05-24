# New repo setup

How to add Agent Fleet to any git repository — from a first local run through optional GitHub PR automation.

**Time estimates** (first repo; later repos are faster once you have patterns):

| Level | What you get | Effort |
|-------|----------------|--------|
| 1 | Global install + `agent-fleet run` | ~15 min |
| 2 | Per-repo scope + verify commands | ~30 min |
| 3 | Composer PR analysis on GitHub Actions | ~1 hr |
| 4 | PR loop (review → fix → CI → merge) | ~2–4 hr (includes bootstrap) |
| 5 | Hermes / Discord dispatch | ~30 min one-time |

Start with [QUICKSTART.md](QUICKSTART.md) for level 1, then return here for repo integration.

---

## Prerequisites

- Python 3.14
- [Cursor API key](https://cursor.com/dashboard/integrations) (`CURSOR_API_KEY`)
- A git repository (local path or clone)
- For GitHub automation: repo admin access to add secrets and workflows
- For PR loop: [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`)

---

## Level 2 — Per-repo config

### Scaffold

```bash
agent-fleet init /absolute/path/to/your/repo
```

This copies `examples/repo.agent-fleet.yaml` → `.agent-fleet.yaml` in the repo.

### Customize

Edit `.agent-fleet.yaml` for your layout:

```yaml
name: my-app
default_persona: coder
default_branch: main

test_command: pytest -q
lint_command: ruff check .

persona_scope_allowlist:
  backend:
    - src/
    - api/
  frontend:
    - web/
    - frontend/

critical_path_prefixes:
  - .github/workflows/
  - infra/
```

**Most important knob:** `persona_scope_allowlist`. Without it, agents can edit anywhere in the monorepo.

For multi-package repos, see `examples/monorepo.agent-fleet.yaml`.

### Verify

```bash
agent-fleet run "Add a docstring to the main entry module" \
  --workspace /absolute/path/to/your/repo \
  --persona backend \
  --pipeline code_review
```

Expect JSON with `phases.execute` and `phases.review`. Status `completed` means implement + review passed.

### Optional: repo-local personas

1. Create markdown under e.g. `agents/personas/backend.md`
2. Set `personas_dir: agents/personas` in `.agent-fleet.yaml`
3. Register persona names in `~/.hermes/coding_fleet/fleet.yaml` (or use bundled `coder` / `reviewer`)

See [PERSONAS.md](PERSONAS.md) for persona authoring.

---

## Level 3 — GitHub PR analyzer

Posts a structured PR review comment on every pull request using Composer (multi-pass: backend/security, frontend, optional quality pass).

### 1. Add `pr_review` to `.agent-fleet.yaml`

```yaml
pr_review:
  enabled: true
  use_in_code_review: true   # also used by local code_review pipeline
  overlay: agents/pr_review_overlay.md
  comment_title: Composer PR Analysis
  area_prefixes:
    frontend: [frontend/, web/, apps/web/]
    backend: [src/, api/, packages/, services/]
```

Copy the overlay template from `examples/agents/pr_review_overlay.md` into your repo and tune it (stack, test commands, security rules).

Full reference config: `examples/repo-full.agent-fleet.yaml`.

### 2. Add GitHub workflow

Copy `examples/github/pr-analyzer.yml` → `.github/workflows/pr-analyzer.yml`.

**Pin the install** to a release tag or commit SHA (do not use floating `@main` in production). See [RELEASE.md](RELEASE.md) for tag format.

```yaml
- uses: astral-sh/setup-uv@v6
  with:
    python-version: "3.14"
- name: Install agent-fleet
  run: uv pip install "git+https://github.com/Evan-Kim2028/agent-fleet.git@v0.6.0"
```

Replace `@v0.6.0` with the version you tested, or `@<40-char-commit-sha>`.

### 3. GitHub secret

In repo **Settings → Secrets → Actions**, add:

| Secret | Value |
|--------|--------|
| `CURSOR_API_KEY` | Your Cursor API key |

For Kimi backend instead of Cursor, set `AGENT_FLEET_BACKEND=kimi` in the workflow and use `KIMI_API_KEY`.

### 4. Permissions

The workflow needs `pull-requests: write` (included in the example). If PR comments fail with permission errors, add `issues: write` under `permissions:`.

### 5. Test

Open a pull request. Within a few minutes you should see a **Composer PR Analysis** comment from the workflow.

Local test (no GitHub):

```bash
agent-fleet pr-review --workspace /path/to/repo --base-branch main
```

---

## Level 4 — PR loop (automated babysitting)

Watches open PRs on configured branch prefixes (default `fleet/*`), addresses blocking review findings, waits for CI, and optionally squash-merges.

### Requirements

Everything from level 3, plus:

```yaml
pr_loop:
  enabled: true
  branch_prefixes: [fleet/]
  poll_interval_s: 10
  review_poll_s: 10
  ci_poll_s: 10
  ci_register_poll_s: 5
  post_fix_poll_s: 15
  fix_persona: coder          # review findings — use a persona that can edit all PR paths
  ci_fix_persona: coder       # CI failures (avoid narrow-scoped personas here)
  auto_merge: true
  max_fix_attempts: 2
  max_ci_fix_attempts: 2
  ignored_ci_checks: []       # e.g. optional linters you want to skip
```

See `examples/repo-full.agent-fleet.yaml` for a commented full block.

### Bootstrap (first time only)

PR loop needs the workflow and `.agent-fleet.yaml` on the default branch. That usually means **one manual PR** that adds:

- `.github/workflows/pr-analyzer.yml`
- `.agent-fleet.yaml` (with `pr_review` + `pr_loop`)
- `agents/pr_review_overlay.md`

Merge that PR yourself (or with admin override). After bootstrap, fleet branches can use automation.

**Protected paths:** PRs that touch paths in `critical_path_prefixes` (commonly `.github/workflows/`) are **parked** for human review instead of auto-merged. Keep bootstrap and workflow changes out of routine `fleet/*` PRs.

### Run the watcher

One-shot poll (good for testing):

```bash
agent-fleet loop --workspace /path/to/repo --once
```

Dry run (no fix dispatch or merge):

```bash
agent-fleet loop --workspace /path/to/repo --once --dry-run
```

Long-running watcher:

```bash
agent-fleet-pr-loop --workspace /path/to/repo
```

Systemd unit template: `examples/agent-fleet-pr-loop.service` (edit paths before enabling).

### Branch convention

Automation targets branches matching `branch_prefixes`, e.g.:

```bash
git checkout -b fleet/fix-login-timeout
# ... push and open PR ...
```

### End-to-end smoke test

1. Bootstrap merged on `main`
2. Create `fleet/test-automation` with a small change **outside** `critical_path_prefixes`
3. Wait for PR analyzer comment + CI
4. Run `agent-fleet loop --workspace ... --once` or start the watcher
5. Confirm fix commit (if needed) and auto-merge

---

## Level 5 — Hermes (optional)

One command from the agent-fleet repo root:

```bash
./scripts/deploy-hermes.sh
```

Enable in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [cursor-fleet]
toolsets:
  - coding_fleet
```

Set `CURSOR_API_KEY` in `~/.hermes/.env`, restart the gateway.

Tools: `coding_fleet_dispatch`, `coding_fleet_pr_review`, `coding_fleet_pr_loop`, `coding_fleet_scope`.

---

## Checklist (copy for each new repo)

```
[ ] pip install -e agent-fleet + CURSOR_API_KEY
[ ] agent-fleet init <repo>
[ ] Edit test_command / lint_command / persona_scope_allowlist
[ ] Smoke: agent-fleet run ... --pipeline code_review
[ ] (Optional) pr_review + agents/pr_review_overlay.md
[ ] (Optional) .github/workflows/pr-analyzer.yml + CURSOR_API_KEY secret
[ ] (Optional) Bootstrap PR merged manually
[ ] (Optional) pr_loop + gh auth + watcher or Hermes coding_fleet_pr_loop
[ ] (Optional) Test fleet/<name> PR auto-merge on non-critical paths
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Workflow installs but analyzer fails | Pin agent-fleet to a known tag/SHA; check `CURSOR_API_KEY` secret |
| No PR comment | Add `issues: write`; confirm workflow ran on `pull_request` |
| `pr_review not configured` | Add `pr_review:` block to `.agent-fleet.yaml` |
| Fix agent can't edit files | Use `fix_persona: coder` (or a persona scoped to PR paths), not a narrow domain persona |
| PR loop never merges | PR may touch `critical_path_prefixes`; check `gh pr checks` for failing required checks |
| `gh: not authenticated` | Run `gh auth login` on the machine running the watcher |
| Agent edits wrong dirs | Tighten `persona_scope_allowlist` |

---

## Related docs

- [QUICKSTART.md](QUICKSTART.md) — install and first run
- [PERSONAS.md](PERSONAS.md) — persona fleet cookbook
- [KIMI.md](KIMI.md) — optional Kimi backend
- `examples/repo.agent-fleet.yaml` — minimal repo config
- `examples/repo-full.agent-fleet.yaml` — PR review + PR loop
- `examples/monorepo.agent-fleet.yaml` — multi-persona monorepo
- `examples/github/pr-analyzer.yml` — GitHub Actions workflow
