---
name: coding-fleet
description: Orchestrate parallel Cursor SDK or Kimi Code CLI coding agents with pluggable personas and pipelines
metadata:
  hermes:
    category: autonomous-ai-agents
    requires_toolsets:
      - coding_fleet
---

# Coding Fleet

When the user mentions **coding fleet** or gives `persona` + `pipeline` for a repo path: call `coding_fleet_dispatch` in your first tool turn.

```json
{
  "goal": "<what to change>",
  "workspace": "/absolute/path/to/repo",
  "persona": "coder",
  "pipeline": "code_review",
  "context": "<constraints, file hints, errors>"
}
```

Tell the user the fleet is running — dispatch usually takes **30–120 seconds**.

## Backend (from fleet.yaml)

| `default_backend` | Key required | Model default |
|-------------------|--------------|---------------|
| `cursor` (default) | `CURSOR_API_KEY` | `composer-2.5` |
| `kimi` (optional) | `KIMI_API_KEY` | `kimi-for-coding` |

Same personas and pipelines for both. Kimi setup: repo `docs/KIMI.md`.

## Pipelines

- `simple` — implement only
- `code_review` — implement then reviewer persona
- `full` — plan → research → implement → verify → review

## Requirements

- Fleet config: `~/.hermes/coding_fleet/fleet.yaml`
- Cursor path: `CURSOR_API_KEY` in `~/.hermes/.env`
- Kimi path: `KIMI_API_KEY` + `kimi-cli` on PATH, `default_backend: kimi`
- `pip install -e /path/to/agent-fleet`
