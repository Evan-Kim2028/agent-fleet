---
name: coding-fleet
description: Orchestrate parallel Cursor SDK coding agents with pluggable personas and pipelines
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

Tell the user the fleet is running — dispatch usually takes **30–90 seconds**.

## Pipelines

- `simple` — implement only
- `code_review` — implement then reviewer persona
- `full` — plan → research → implement → verify → review

## Requirements

- `CURSOR_API_KEY` in environment
- `pip install -e /path/to/agent_fleet`
