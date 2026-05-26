## Role

Implement **PR3**: wire `resolve_dispatch_equip()` + composed persona body into PR loop fix agents and code_review auto-fix.

## Scope

- `agent_fleet/pr_loop/lifecycle.py` — `address_review_findings`, `attempt_ci_fix`
- `agent_fleet/code_review/fix.py`
- `tests/test_pr_loop_equip.py`, `tests/test_code_review_fix_equip.py`

## Branch

**`feature/skills-pr-loop`**

## Plan

PR3 section in `docs/superpowers/plans/2026-05-25-skills-integration-buildout.md`.

## Done when

- Fix-agent prompts include equip compose body (skills visible in prompt)
- Tests prove fix-ci / skill markers present
- `pytest -q` green on branch
