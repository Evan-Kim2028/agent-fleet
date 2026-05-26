#!/usr/bin/env bash
# Fleet worktree bootstrap — run after git worktree add on a target repo.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
frontend="${repo_root}/frontend"

if [[ ! -d "$frontend" ]]; then
  exit 0
fi

cd "$frontend"

if [[ ! -e node_modules/.bin/eslint ]]; then
  if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
    main_repo="$(dirname "$(realpath "$git_common_dir")")"
    main_nm="${main_repo}/frontend/node_modules"
    if [[ -d "$main_nm" && "$main_nm" != "$(pwd)/node_modules" ]]; then
      ln -sfn "$main_nm" node_modules
    fi
  fi
fi

if [[ ! -e node_modules/.bin/eslint ]]; then
  echo "fleet bootstrap: installing frontend deps (eslint missing)..." >&2
  if [[ -f package-lock.json ]]; then
    npm ci --no-audit --no-fund
  else
    npm install --no-audit --no-fund
  fi
fi
