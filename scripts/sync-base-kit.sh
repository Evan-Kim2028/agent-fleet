#!/usr/bin/env bash
# Vendor superpowers + pstack skills into agent_fleet/base-kit/. Updates manifest SHAs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$ROOT/agent_fleet/base-kit"
MANIFEST="$BASE/manifest.yaml"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

log() { printf '[sync-base-kit] %s\n' "$*" >&2; }

clone_sha() {
  local repo="$1"
  local git_dir="$2"
  git clone --depth 1 "https://github.com/${repo}.git" "$git_dir" >&2
  git -C "$git_dir" rev-parse HEAD
}

sync_superpowers() {
  log "Syncing obra/superpowers → base-kit/superpowers/"
  local repo_dir="$TMP/superpowers"
  local sha
  sha="$(clone_sha obra/superpowers "$repo_dir")"
  rm -rf "$BASE/superpowers"
  mkdir -p "$BASE/superpowers"
  rsync -a --delete "$repo_dir/skills/" "$BASE/superpowers/"
  printf '%s' "$sha"
}

sync_pstack() {
  log "Syncing cursor/plugins pstack → base-kit/pstack/"
  local repo_dir="$TMP/plugins"
  local sha
  sha="$(clone_sha cursor/plugins "$repo_dir")"
  rm -rf "$BASE/pstack"
  mkdir -p "$BASE/pstack"
  rsync -a --delete "$repo_dir/pstack/skills/" "$BASE/pstack/"
  printf '%s' "$sha"
}

sync_cursor_team_kit() {
  log "Syncing cursor/plugins cursor-team-kit → base-kit/cursor-team-kit/"
  local repo_dir="$TMP/plugins-ctk"
  local sha
  sha="$(clone_sha cursor/plugins "$repo_dir")"
  rm -rf "$BASE/cursor-team-kit"
  mkdir -p "$BASE/cursor-team-kit"
  rsync -a --delete "$repo_dir/cursor-team-kit/skills/" "$BASE/cursor-team-kit/"
  printf '%s' "$sha"
}

write_manifest() {
  local super_sha="$1"
  local pstack_sha="$2"
  local ctk_sha="$3"
  cat >"$MANIFEST" <<EOF
schema_version: 1
synced_at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sources:
  superpowers:
    upstream: https://github.com/obra/superpowers
    ref: ${super_sha}
    license: MIT
  pstack:
    upstream: https://github.com/cursor/plugins/tree/main/pstack
    ref: ${pstack_sha}
    license: MIT
  cursor-team-kit:
    upstream: https://github.com/cursor/plugins/tree/main/cursor-team-kit/skills
    ref: ${ctk_sha}
    license: MIT
EOF
  log "Wrote $MANIFEST"
}

main() {
  command -v git >/dev/null
  command -v rsync >/dev/null
  super_sha="$(sync_superpowers)"
  pstack_sha="$(sync_pstack)"
  ctk_sha="$(sync_cursor_team_kit)"
  write_manifest "$super_sha" "$pstack_sha" "$ctk_sha"
  log "Done. superpowers=${super_sha:0:12} pstack=${pstack_sha:0:12} cursor-team-kit=${ctk_sha:0:12}"
}

main "$@"
