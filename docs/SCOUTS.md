# Fleet Scouts

Read-only intake layer: **product discovery** + **technical repo map** → **ScoutBrief** → engineering scope → dispatch.

## Quick start

```bash
agent-fleet scout --workspace /path/to/repo
agent-fleet scout --workspace /path/to/repo --product-context "Billing for SMB users"
agent-fleet scope --workspace /path/to/repo   # rank engineering tasks
agent-fleet run "..." --workspace /path/to/repo --pipeline code_review
```

Hermes: `coding_fleet_scout` → `coding_fleet_scope` → `coding_fleet_dispatch`

## Depth

| Mode | Default | Behavior |
|------|---------|----------|
| `light` | ✅ | Product scout + parallel tech scout shards → synthesize |
| `deep` | | Reserved for multi-hop research follow-ups |

## Code review auto-fix

When `pr_loop.enabled: true`, `code_review` inherits:

- `auto_fix` — re-run fix persona on `request_changes` / `verify_failed`
- `auto_push` — push `fleet/*` worktree branch and open PR
- `auto_pr_loop` — run review → CI → merge lifecycle on that PR

Override in `.agent-fleet.yaml`:

```yaml
code_review:
  auto_fix: true
  max_fix_attempts: 2
  fix_persona: coder
  auto_push: true
  auto_pr_loop: true
```

See [NEW-REPO.md](NEW-REPO.md) for full repo setup.
