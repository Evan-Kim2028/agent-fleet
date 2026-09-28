#!/usr/bin/env bash
# gate_queue_run.sh v2 — gate driver + escalation router (on-box substitute for
# the off-box orchestrator: fbgate_remote + fleet_reconcile, quiet since ~09:00).
#
# Serialisation is by TEST BURST, not by gate: fm_pytest.sh already flocks
# ~/fleet/fb/sem/test.* (TEST_SLOTS=1 in env.sh -> one 6G pytest burst globally).
# So up to MAX_GATES gates run concurrently — their find/judge phases are just
# LLM calls (~200MB/agent, bounded by GATE_AGENT_SLOTS sem) and only ever queue
# on the shared test slot. MAX_GATES stays at 3: 3 gates of idle agents (~1.5G)
# + one 6G burst fits the 10G fleet.slice.
#
# Escalation routing (ported from fleet_reconcile.sh): each pass scans lane
# statuses for lanes that are neither queued nor gating:
#   INFRA  — shed/fail-closed/agent-died/test-infra -> re-queue (cap 3/head)
#   REWORK — no-push/stalled-fixer/merged-tree-regression/untestable -> one
#            fresh gate per head (cap 1/head)
#   REBASE — NEEDS-REBASE stays unqueued (rebase-agent path not yet on-box)
#   approved/verdict lanes are left alone; the merge train owns those.
# A lane already in the queue file or currently gating is never re-added.
set -uo pipefail
F=$HOME/fleet/fb; S=$HOME/fleet/state; Q=$F/gate_queue.txt; DONE=$F/gate_queue_done.txt
RQ=$F/.requeue_counts; touch $RQ
MAXG=${MAX_GATES:-3}
DGRH=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/lor.slice/lor-dagster.slice/lor-dagster-run.slice/lor-dagster-run-heavy.slice/memory.current
log(){ logger -t gate-queue "$*"; echo "$(date +%T) $*" >> $F/gate_queue.log; }
gate_units(){ systemctl --user list-units --no-legend --plain "fleet-gate-*.service" --state=running,activating 2>/dev/null | awk "{print \$1}" | grep -v fleet-gate-queue.service; }
live_gates(){ { gate_units; pgrep -f "fleet/fb/fbgate " | sed "s/^/pg:/"; } | grep -v "^$" | sort -u; }
queued(){ grep -v "^#" $Q 2>/dev/null | awk "{print \$1}"; }
requeues(){ grep -c " $1 " $RQ 2>/dev/null || echo 0; }
find_pr(){ # find_pr LANE -> "REPO PR" or empty
  for r in lake-of-rage silphcoanalytics agent-fleet; do
    pr=$(gh pr list -R Evan-Kim2028/$r --limit 100 --state open --json number,headRefName --jq ".[] | select(.headRefName==\"fb/$1\" or .headRefName==\"dq1d/${1#dq1d-}\") | .number" 2>/dev/null | head -1)
    [ -n "$pr" ] && { echo "$r $pr"; return; }
  done
}
scan_escalations(){
  local l last cls n lim repo pr
  for s in $F/lanes/*.status; do
    l=$(basename $s .status)
    gate_units | grep -qx "fleet-gate-$l.service" && continue
    queued | grep -qx "$l" && continue
    last=$(tail -1 $s 2>/dev/null)
    case "$last" in
      *"start @"*|*PREMERGE-APPROVED*|*NEEDS-REBASE*) continue;;
    esac
    cls=""
    case "$last" in
      *"shed by fleet pressure"*|*"fail-closed"*|*"died"*|*"could not run"*|*"gate refused"*|*"gate stuck"*) cls=infra;;
      *"no-push"*|*"stalled after"*|*"merged-tree regression"*|*"untestable"*|*"fix round pushed nothing"*) cls=rework;;
    esac
    [ -z "$cls" ] && continue
    lim=3; [ "$cls" = rework ] && lim=1
    n=$(requeues $l); [ "$n" -ge "$lim" ] && continue
    read -r repo pr < <(find_pr $l); [ -n "${pr:-}" ] || continue
    echo "$l $repo $pr" >> $Q; echo "$l $(date +%s) $cls" >> $RQ
    log "re-queue $l ($repo#$pr) class=$cls (${n} prior)"
  done
}
while :; do
  line=""
  while read -r l; do [ -n "${l%%#*}" ] || continue; line=${l%%#*}; break; done < $Q 2>/dev/null
  live=$(live_gates); nl=$(echo "$live" | grep -c . || true)
  scan_escalations
  if [ -z "$line" ]; then
    idle_n=$(cat $S/.gq_idle 2>/dev/null || echo 0); idle_n=$((idle_n+1)); echo $idle_n > $S/.gq_idle
    [ $idle_n -gt 200 ] && { log "nothing actionable for ~5h; exit"; exit 0; }
    sleep 90; continue
  fi
  echo 0 > $S/.gq_idle
  adm=$(cat $S/admission 2>/dev/null || echo closed)
  dgr=$(cat $DGRH 2>/dev/null || echo 0)
  if [ "$adm" = open ] && [ "$nl" -lt "$MAXG" ] && [ "${dgr:-0}" -lt 104857600 ]; then
    read -r LANE REPO PR <<EOF2
$line
EOF2
    if [ -n "${LANE:-}" ]; then
      systemctl --user reset-failed fleet-gate-$LANE.service 2>/dev/null
      if systemd-run --user --quiet --collect --unit=fleet-gate-$LANE --slice=fleet.slice -p RuntimeMaxSec=21600 \
        -p StandardOutput=append:$F/gate-$LANE.log -p StandardError=append:$F/gate-$LANE.log \
        /bin/bash -lc "~/fleet/fb/fbgate $LANE $REPO $PR"; then
        sed -i "/^$LANE $REPO $PR\$/d" $Q
        echo "$LANE $REPO $PR $(date +%F\ %T)" >> $DONE
        log "fired gate $LANE ($REPO#$PR) [$nl in flight]"
        sleep 10
      else
        log "fire failed $LANE; retrying next tick"
      fi
    else
      sed -i "1d" $Q
    fi
  fi
  sleep 90
done
