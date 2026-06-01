#!/usr/bin/env bash
# Fleet worktree bootstrap — run after git worktree add on a target repo.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
frontend="${repo_root}/frontend"

main_repo=""
if git_common_dir="$(git rev-parse --git-common-dir 2>/dev/null)"; then
  main_repo="$(dirname "$(realpath "$git_common_dir")")"
fi

# Share main-repo Python venvs when worktree lacks its own (fleet verify runs api ruff).
if [[ -n "$main_repo" ]]; then
  for component in api pipeline; do
    work_venv="${repo_root}/${component}/.venv"
    main_venv="${main_repo}/${component}/.venv"
    if [[ ! -e "$work_venv/bin/ruff" && -d "$main_venv" ]]; then
      ln -sfn "$main_venv" "$work_venv"
    fi
  done
fi

if [[ ! -d "$frontend" ]]; then
  exit 0
fi

cd "$frontend"

if [[ ! -e node_modules/.bin/eslint ]]; then
  if [[ -n "$main_repo" ]]; then
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
