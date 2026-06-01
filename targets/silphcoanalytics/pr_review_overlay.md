# Two-pass PR analyzer — SilphCo Analytics domain invariants

## Agent fleet (protected)

- Do not modify `agents/agents/`, `agents/silphco/`, or agent-fleet target config (`targets/silphcoanalytics.*`) without explicit allow-label — these are critical paths.
- Issue dispatch uses `/agent --persona <name>` on GitHub issues; fleet mutex labels are not triggers.

## Backend (FastAPI)

- API changes in `api/` must keep auth middleware and MCP tool contracts aligned with `docs/reference/mcp.md`.
- Polars/DuckDB query paths in `pipeline/src/queries/` must not break existing API response shapes without migration notes.

## Frontend

- React 19 + Tailwind 4 in `frontend/` — match existing component patterns; no drive-by refactors outside the PR scope.
- Chart/data hooks must use the same API contracts as `api/` routes.

## Pipeline / data

- Raw → silver → gold ETL in `pipeline/` must preserve fixture contracts under `data/fixtures/` when tests exist.
- Cron/watcher scripts in `watcher/` and `ops/deploy/` are ops-critical — flag scheduling or secret-handling changes as HIGH.

## Security

- No secrets, API keys, or credentials in diff. SSH/deploy secrets stay in GitHub Actions secrets only.
- Auth bypass or MCP tool scope expansion requires explicit security review.

## Tests

- Backend: `cd api && pytest` (or scoped tests for touched modules).
- Agents: `cd agents && python -m pytest tests -q` when `agents/` changes.
- Frontend: run relevant vitest/eslint when `frontend/` changes.
