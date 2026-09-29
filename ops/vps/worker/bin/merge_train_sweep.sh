#!/usr/bin/env bash
set -uo pipefail
CLI=${FLEET_AGENT_CLI:-$HOME/fleet/src/agent-fleet/.venv/bin/agent-fleet}
STATUS_DIR=${FLEET_STATUS_DIR:-$HOME/fleet/fb/lanes}
repos=(
  "lake-of-rage:$HOME/lake-of-rage"
  "silphcoanalytics:$HOME/silphcoanalytics"
  "agent-fleet:$HOME/fleet/src/agent-fleet"
)
rc=0
for spec in "${repos[@]}"; do
  repo=${spec%%:*}
  path=${spec#*:}
  [ -d "$path" ] || continue
  "$CLI" merge train \
    --repo-path "$path" \
    --repo "$repo" \
    --status-dir "$STATUS_DIR" \
    --include-head-prefix fb/ \
    --include-head-prefix dq1d/ \
    --max-batch-size 3 \
    --json || rc=1
done
exit "$rc"
