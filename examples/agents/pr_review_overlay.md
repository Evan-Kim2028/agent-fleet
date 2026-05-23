# PR review overlay

Repo-specific context injected into Composer PR analysis. Tune for your stack.

## Stack

- Language/runtime: (e.g. Python 3.12, Node 20)
- Package manager: (e.g. pip, uv, pnpm)
- Test command: `pytest -q` (or your CI equivalent)
- Lint command: `ruff check .`

## Architecture

- Brief description of main packages and boundaries
- Which paths are user-facing vs internal vs infra

## Review priorities

1. Correctness and regressions in changed code paths
2. Security: auth, input validation, secrets, SQL/injection
3. Tests for behavior changes
4. Minimal diff — no drive-by refactors

## Do not flag

- Lockfile-only changes unless dependency is suspicious
- Markdown/docs-only PRs unless factual errors

## Merge blockers

- Failing tests or type errors introduced by the PR
- Secrets or credentials in diff
- Breaking API changes without migration notes (if applicable)
