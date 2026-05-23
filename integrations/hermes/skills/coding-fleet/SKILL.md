---
name: coding-fleet
description: Dispatch scoped coding personas through Agent Fleet review pipelines — parallel runs from Hermes with pluggable execution backends
metadata:
  hermes:
    category: autonomous-ai-agents
    requires_toolsets:
      - coding_fleet
---

# Coding Fleet

When the user mentions **coding fleet**, **PR analyzer**, or gives `persona` + `pipeline` for a repo path: call the appropriate tool in your first turn.

## PR loop (review → fix → CI → merge)

For repos with `pr_loop.enabled: true` in `.agent-fleet.yaml` (e.g. lake-of-rage after bootstrap):

**Poll all open `fleet/*` PRs once:**

```json
{
  "workspace": "/home/evan/Documents/lake-of-rage",
  "mode": "once"
}
```

**Run full lifecycle for one PR** (address review findings → wait CI → merge):

```json
{
  "workspace": "/home/evan/Documents/lake-of-rage",
  "mode": "pr",
  "pr_number": 49,
  "skip_review_wait": true
}
```

Tool: `coding_fleet_pr_loop`

Requires `gh` authenticated, `CURSOR_API_KEY`, and `pr_loop` + `pr_review` in repo config. PRs touching `.github/workflows/` auto-park for human review.

## Scope fleet work (thermo-nuclear quality review)

Rank dispatchable tasks from open issues + large-file hotspots:

```json
{
  "workspace": "/home/evan/Documents/silphcoanalytics",
  "github_repo": "Evan-Kim2028/silphcoanalytics",
  "issue_limit": 20
}
```

Tool: `coding_fleet_scope`

Then dispatch top-ranked tasks with `coding_fleet_dispatch`.

PR reviews automatically include a **quality pass** using the bundled `thermo-nuclear-code-quality-review` skill (maintainability, 1k-line guardrails, code-judo).

## Implement / review dispatch

```json
{
  "goal": "<what to change>",
  "workspace": "/absolute/path/to/repo",
  "persona": "coder",
  "pipeline": "code_review",
  "context": "<constraints, file hints, errors>"
}
```

Repos with `pr_review.use_in_code_review: true` in `.agent-fleet.yaml` automatically use the **two-pass PR analyzer** (Composer 2.5) for the review phase instead of the generic reviewer.

## PR analyzer only (no implementer)

Use when reviewing an existing branch or worktree diff:

```json
{
  "workspace": "/absolute/path/to/repo",
  "base_branch": "main",
  "output_format": "json"
}
```

Tool: `coding_fleet_pr_review`

Or dispatch with pipeline `pr_review`:

```json
{
  "goal": "Analyze current branch diff",
  "workspace": "/absolute/path/to/repo",
  "persona": "pr-analyzer",
  "pipeline": "pr_review"
}
```

## Execution backend (from fleet.yaml)

| `default_backend` | Key required | Model default |
|-------------------|--------------|---------------|
| `cursor` (default) | `CURSOR_API_KEY` | `composer-2.5` |
| `kimi` (optional) | `KIMI_API_KEY` | `kimi-for-coding` |

## Pipelines

- `simple` — implement only
- `code_review` — implement → scope → verify? → **PR analyzer review** (when repo configured)
- `pr_review` — analyze diff only (two-pass backend/security + frontend)
- `full` — plan → research → implement → verify → review

## Repo tuning

Add to `.agent-fleet.yaml`:

```yaml
pr_review:
  enabled: true
  use_in_code_review: true
  overlay: agents/pr_review_overlay.md
  area_prefixes:
    frontend: [frontend/, web/]
    backend: [packages/, pipelines/, api/]
```

## Requirements

- Fleet config: `~/.hermes/coding_fleet/fleet.yaml`
- Cursor SDK backend: `CURSOR_API_KEY` in `~/.hermes/.env`
- Kimi backend: `KIMI_API_KEY` + `kimi-cli` on PATH, `default_backend: kimi`
- `pip install -e /path/to/agent-fleet`
