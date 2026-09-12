# Release tags

How we cut and name agent-fleet releases. Follow this for every release going forward.

**Current published tag:** [v0.15.2](https://github.com/Evan-Kim2028/agent-fleet/releases/tag/v0.15.2) (2026-09-12). Git tags without a GitHub Release do not show up as Latest on the Releases page.

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
| `uv.lock` | `name = "agent-fleet"` / `version` (run `uv lock` after the bump) |
| `tests/test_p6_docs_version.py` | hardcoded current version in three asserts |
| `CHANGELOG.md` | `## {VERSION}` section |

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
VERSION=0.15.2

# 2. Changelog + p6 version-gate tests (cut_release.sh does not touch these)
#    Edit CHANGELOG.md Unreleased → ## $VERSION
#    Update the three hardcoded versions in tests/test_p6_docs_version.py

# 3. Helper syncs pyproject.toml + __init__.py, runs ruff/ty/pytest, commits, tags
./scripts/cut_release.sh "$VERSION" \
  "highlight one" \
  "highlight two"

# 4. If uv.lock still shows the previous version, amend it into the release commit
uv lock
git add uv.lock
git commit --amend --no-edit   # only if the tag is not pushed yet
# If the tag already exists locally: git tag -d v$VERSION && git tag -a v$VERSION -m "..."

# 5. Push branch + tag
git push origin main --tags

# 6. GitHub Release (this is what the Releases page shows as Latest)
gh release create "v${VERSION}" \
  --title "v${VERSION}" \
  --notes-file /tmp/agent-fleet-v${VERSION}-notes.md \
  --latest
```

`cut_release.sh` does **not** create a GitHub Release. A pushed annotated tag alone leaves the Releases page on an older Latest.

Dry-run (no tag, no file writes):

```bash
./scripts/cut_release.sh --dry-run 0.15.2 "preview only"
```

## Pinning in downstream repos

Production installs should pin to a release tag or commit SHA — never floating `@main`:

```bash
pip install "git+https://github.com/Evan-Kim2028/agent-fleet.git@v0.15.2"
```

In GitHub Actions, prefer `astral-sh/setup-uv@v6` with `python-version: "3.14"` and the same git pin.

## Pre-release checklist

1. `main` CI is green (lint, typecheck, test).
2. `uv run ruff format --check`, `uv run ruff check`, `uv run ty check`, `uv run pytest` pass locally.
3. Version bumped in every source of truth (including `tests/test_p6_docs_version.py`, `CHANGELOG.md`, `uv.lock`).
4. Annotated tag created via `scripts/cut_release.sh`.
5. Tag pushed; `gh release create v{VERSION} --latest` so GitHub Releases matches the tag.
6. Downstream pin examples (`docs/NEW-REPO.md`, `examples/github/pr-analyzer.yml`) point at the new tag.
