# Release tags

How we cut and name agent-fleet releases. Follow this for every release going forward.

## Tag format

```
v{MAJOR}.{MINOR}.{PATCH}
```

Examples: `v0.6.0`, `v0.6.1`, `v1.0.0`.

Rules:

- **Semver, no zero-padding** — tag names match `pyproject.toml` / `__version__` exactly (`0.6.0` → `v0.6.0`, not `v0.06.00`).
- **Annotated tags only** — lightweight tags are not allowed for releases. Annotated tags carry a skimmable header in `git tag -n3`.
- **One tag per version** — never retag or move an existing release tag.

### Annotated tag message (skimmable header)

Use this fixed first line so release lists align when you run `git tag -l -n1 --sort=-v:refname`:

```
agent-fleet v{MAJOR}.{MINOR}.{PATCH} | {YYYY-MM-DD} | python 3.14
```

Body (optional but recommended):

```
Highlights:
- bullet one
- bullet two
```

Example:

```
agent-fleet v0.6.0 | 2026-05-24 | python 3.14

Highlights:
- Python 3.14 only
- CI: ruff, ty, pytest gate on main
```

## Version sources of truth

These must match before you tag:

| File | Field |
|------|-------|
| `pyproject.toml` | `[project].version` |
| `agent_fleet/__init__.py` | `__version__` |

## Commit conventions

- **Do not** embed release versions in everyday commits (`feat(v0.5.10): ...` is deprecated).
- Use conventional commits on `main`: `feat:`, `fix:`, `docs:`, `chore:`.
- Bump version in a dedicated commit immediately before tagging:

  ```
  chore: release v0.6.0
  ```

## Cut a release

From a green `main`:

```bash
# 1. Set VERSION (semver, no leading v)
VERSION=0.6.0

# 2. Run the helper (syncs version files, runs checks, creates annotated tag)
./scripts/cut_release.sh "$VERSION" \
  "Python 3.14 only" \
  "CI: ruff, ty, pytest gate on main"

# 3. Push branch + tag
git push origin main --tags
```

Dry-run (no tag, no file writes):

```bash
./scripts/cut_release.sh --dry-run 0.6.0 "preview only"
```

## Pinning in downstream repos

Production installs should pin to a release tag or commit SHA — never floating `@main`:

```bash
pip install "git+https://github.com/Evan-Kim2028/agent-fleet.git@v0.6.0"
```

In GitHub Actions, prefer `astral-sh/setup-uv@v6` with `python-version: "3.14"` and the same git pin.

## Pre-release checklist

1. `main` CI is green (lint, typecheck, test).
2. `uv run ruff format --check`, `uv run ruff check`, `uv run ty check`, `uv run pytest` pass locally.
3. Version bumped in both sources of truth.
4. Annotated tag created via `scripts/cut_release.sh`.
5. Tag pushed; downstream pins updated if needed.
