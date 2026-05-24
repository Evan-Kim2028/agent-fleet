#!/usr/bin/env bash
# Create an annotated agent-fleet release tag with a skimmable header.
#
# Usage:
#   ./scripts/cut_release.sh 0.6.0 "highlight one" "highlight two"
#   ./scripts/cut_release.sh --dry-run 0.6.0
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  echo "usage: $0 [--dry-run] VERSION [highlight ...]" >&2
  exit 1
fi
shift

if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION must be semver MAJOR.MINOR.PATCH (got: $VERSION)" >&2
  exit 1
fi

TAG="v${VERSION}"
DATE="$(date +%Y-%m-%d)"
HEADER="agent-fleet v${VERSION} | ${DATE} | python 3.14"

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "tag already exists: $TAG" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree is not clean; commit or stash changes first" >&2
  exit 1
fi

PYPROJECT="$ROOT/pyproject.toml"
INIT_PY="$ROOT/agent_fleet/__init__.py"

sync_version() {
  sed -i "s/^version = \".*\"/version = \"${VERSION}\"/" "$PYPROJECT"
  sed -i "s/^__version__ = \".*\"/__version__ = \"${VERSION}\"/" "$INIT_PY"
}

verify_version_sync() {
  local pyproject_version init_version
  pyproject_version="$(sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT")"
  init_version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INIT_PY")"
  if [[ "$pyproject_version" != "$VERSION" || "$init_version" != "$VERSION" ]]; then
    echo "version mismatch: pyproject=$pyproject_version __init__=$init_version expected=$VERSION" >&2
    exit 1
  fi
}

run_checks() {
  uv sync --frozen --group dev
  uv run ruff format --check agent_fleet tests integrations
  uv run ruff check agent_fleet tests integrations
  uv run ty check agent_fleet tests integrations
  uv run pytest -q
}

build_message() {
  local msg="$HEADER"
  if (("$#")); then
    msg+=$'\n\nHighlights:\n'
    for line in "$@"; do
      msg+="- ${line}"$'\n'
    done
  fi
  printf '%s' "$msg"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[dry-run] would set version to $VERSION"
  echo "[dry-run] would run ruff, ty, pytest"
  echo "[dry-run] would create annotated tag $TAG with message:"
  echo "---"
  build_message "$@"
  echo "---"
  exit 0
fi

sync_version
verify_version_sync
run_checks

git add "$PYPROJECT" "$INIT_PY"
git commit -m "$(cat <<EOF
chore: release ${TAG}

EOF
)"

MESSAGE="$(build_message "$@")"
git tag -a "$TAG" -m "$MESSAGE"

echo "created $TAG on $(git rev-parse --short HEAD)"
echo "$HEADER"
