#!/usr/bin/env bash
# lane_impl.sh LANE REPO — run `agent-fleet lane run` for a task-file lane,
# then append the lane to gate_queue.txt so the driver gates the PR it made.
set -uo pipefail
LANE=$1; REPO=$2
F=$HOME/fleet/fb
LOG=$F/lane-impl-$LANE.log
cd ~/fleet/src/agent-fleet || exit 65
source ~/fleet/env.sh 2>/dev/null || true
export FLEET_CAPACITY_HELPER="${FLEET_CAPACITY_HELPER:-$HOME/fleet/bin/fleet_admission.py}"
~/fleet/src/agent-fleet/.venv/bin/agent-fleet lane run \
  --operator devin-0 --lane "$LANE" --repo-path "$HOME/$REPO" \
  --task-file "$F/prompts/$LANE.task.md" --engine cmd \
  --worktree-parent "$HOME/fleet/wt" --no-gate \
  --status-file "$F/lanes/$LANE.status" > "$LOG" 2>&1
rc=$?
# if a PR now exists for fb/LANE (or dq1d/x), queue the gate
pr=$(gh pr list -R Evan-Kim2028/$REPO --state open --limit 50 --json number,headRefName --jq ".[] | select(.headRefName==\"fb/$LANE\" or .headRefName==\"dq1d/${LANE#dq1d-}\") | .number" 2>/dev/null | head -1)
if [ -n "$pr" ]; then
  printf "%s %s %s\n" "$LANE" "$REPO" "$pr" >> "$F/gate_queue.txt"
  logger -t gate-queue "lane_impl $LANE produced PR #$pr; queued for gate"
else
  logger -t gate-queue "lane_impl $LANE rc=$rc, no PR produced (see $LOG)"
fi
exit $rc
