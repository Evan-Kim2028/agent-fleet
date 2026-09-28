#!/usr/bin/env bash
# gate_queue_run.sh - serial lane gate driver. On-box substitute for the off-box
# fbgate_remote orchestrator (quiet since ~09:00). Pops "LANE REPO PR" lines from
# ~/fleet/fb/gate_queue.txt and starts fleet-gate-LANE via systemd-run when the
# box has room: no gate already running, admission open, no heavy Dagster run
# (fleet.slice freezes during one anyway, so firing then just wastes FB_CAP).
# Finished/fired lanes append to gate_queue_done.txt so restarts resume cleanly.
set -uo pipefail
F=$HOME/fleet/fb; S=$HOME/fleet/state; Q=$F/gate_queue.txt; DONE=$F/gate_queue_done.txt
DGRH=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/lor.slice/lor-dagster.slice/lor-dagster-run.slice/lor-dagster-run-heavy.slice/memory.current
log(){ logger -t gate-queue "$*"; echo "$(date +%T) $*" >> $F/gate_queue.log; }
idle=0
while :; do
  line=""
  while read -r l; do [ -n "${l%%#*}" ] || continue; line=${l%%#*}; break; done < $Q 2>/dev/null
  if [ -z "$line" ]; then idle=$((idle+1)); [ $idle -gt 40 ] && { log "queue drained; exit"; exit 0; }; sleep 90; continue; fi
  idle=0
  adm=$(cat $S/admission 2>/dev/null || echo closed)
  gate=$(systemctl --user list-units --no-legend --plain "fleet-gate-*.service" --state=running,activating 2>/dev/null | awk "{print $1}" | grep -v fleet-gate-queue.service | head -1)
  dgr=$(cat $DGRH 2>/dev/null || echo 0)
  if [ "$adm" = open ] && [ -z "$gate" ] && [ "${dgr:-0}" -lt 104857600 ]; then
    read -r LANE REPO PR <<EOF2
$line
EOF2
    if [ -n "${LANE:-}" ]; then
      systemctl --user reset-failed fleet-gate-$LANE.service 2>/dev/null
      if systemd-run --user --quiet --collect --unit=fleet-gate-$LANE --slice=fleet.slice -p RuntimeMaxSec=21600 \
        -p StandardOutput=append:$F/gate-$LANE.log -p StandardError=append:$F/gate-$LANE.log \
        /bin/bash -lc "~/fleet/fb/fbgate $LANE $REPO $PR"; then
        sed -i "/^$LANE $REPO $PR$/d" $Q
        echo "$LANE $REPO $PR $(date +%F\ %T)" >> $DONE
        log "fired gate $LANE ($REPO#$PR)"
      else
        log "fire failed $LANE; retrying next tick"
      fi
    else
      sed -i "1d" $Q
    fi
  fi
  sleep 90
done
