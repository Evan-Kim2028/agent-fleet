#!/usr/bin/env bash
# Fleet worktree bootstrap — run after git worktree add on a target repo.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
frontend="${repo_root}/frontend"

# Ensure user tool installs (uv tool / pip --user) are on PATH for hooks.
export PATH="${HOME}/.local/bin:${HOME}/bin:${PATH}"

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

# --- pre-commit (required when .pre-commit-config.yaml is present) ---
# pr_loop commit preflight runs `pre-commit run --files ...` and git hooks
# invoke the same binary. Missing binary → FileNotFoundError and parked PR.
ensure_pre_commit() {
  if command -v pre-commit >/dev/null 2>&1; then
    return 0
  fi
  echo "fleet bootstrap: pre-commit missing; installing..." >&2
  if command -v uv >/dev/null 2>&1; then
    uv tool install pre-commit || true
  fi
  if ! command -v pre-commit >/dev/null 2>&1 && command -v pipx >/dev/null 2>&1; then
    pipx install pre-commit || true
  fi
  if ! command -v pre-commit >/dev/null 2>&1; then
    python3 -m pip install --user pre-commit || true
  fi
  if ! command -v pre-commit >/dev/null 2>&1; then
    echo "fleet bootstrap: ERROR: pre-commit still not on PATH after install attempts" >&2
    echo "  fix: uv tool install pre-commit  (or pip install --user pre-commit)" >&2
    return 1
  fi
  echo "fleet bootstrap: pre-commit installed at $(command -v pre-commit)" >&2
}

if [[ -f "${repo_root}/.pre-commit-config.yaml" ]]; then
  ensure_pre_commit
  # Install hooks into the shared git dir so worktree commits run them.
  # pre-commit refuses when core.hooksPath is set (even to the default
  # .git/hooks); fleet commit preflight still runs `pre-commit run` directly.
  hooks_path="$(git -C "$repo_root" config --get core.hooksPath 2>/dev/null || true)"
  if [[ -n "$hooks_path" ]]; then
    echo "fleet bootstrap: core.hooksPath=$hooks_path — skipping pre-commit install (preflight uses pre-commit run)" >&2
  else
    (cd "$repo_root" && pre-commit install --install-hooks) || {
      echo "fleet bootstrap: warning: pre-commit install failed (commit preflight may still run)" >&2
    }
  fi
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
