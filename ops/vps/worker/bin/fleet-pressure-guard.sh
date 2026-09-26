#!/bin/bash
# fleet-pressure-guard v3 (owner 2026-09-26: throttle, don't freeze). Runs every 30 s (fleet-pressure-guard.timer).
# Graduated by host state (MemAvailable GiB, memory PSI full avg60 %):
#   level 0 calm      : admission open, CPUQuota 600%, MemoryHigh as configured
#   level 1 mild      : avail < 12 or PSI > 10  -> admission CLOSED (no new gates; running ones continue)
#   level 2 moderate  : avail <  8 or PSI > 25  -> + CPUQuota 200% and MemoryHigh pinned at current usage (slows, never stops)
#   level 3 severe    : avail <  4              -> + stop the newest remote gate each tick (frees memory; re-queued by the orchestrator)
#   level 4 emergency : avail <  2              -> freeze fleet.slice (last resort before the kernel OOM killer)
# Relaxes one level at a time after 5 min at a calmer level. Every change is logged (logger -t fleet-guard) and
# exported in ~/fleet/state/{level,admission} for fbgate_remote / the orchestrator.
S=~/fleet/state; mkdir -p $S; U=fleet.slice
C=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/fleet.slice
full=$(awk '/^full/{split($3,a,"="); print a[2]}' /proc/pressure/memory)
avail=$(awk '/^MemAvailable/{print int($2/1048576)}' /proc/meminfo)
gt(){ awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>b)}'; }
want=0
{ [ $avail -lt 12 ] || gt "$full" 10; } && want=1
{ [ $avail -lt 8 ]  || gt "$full" 25; } && want=2
[ $avail -lt 4 ] && want=3
[ $avail -lt 2 ] && want=4
cur=$(cat $S/level 2>/dev/null || echo 0)
if [ $want -ge $cur ]; then new=$want; rm -f $S/calm_since
else  # relax one level after 5 calm minutes
  [ -f $S/calm_since ] || date +%s > $S/calm_since
  if [ $(( $(date +%s) - $(cat $S/calm_since) )) -ge 300 ]; then new=$((cur-1)); date +%s > $S/calm_since; else new=$cur; fi
fi
log(){ logger -t fleet-guard "level $cur->$new (avail=${avail}G psi_full=$full): $*"; }
[ $new -ge 1 ] && echo closed > $S/admission || echo open > $S/admission
if [ $new -ge 2 ] && [ $cur -lt 2 ]; then
  systemctl --user set-property --runtime $U CPUQuota=200% MemoryHigh=$(cat $C/memory.current); log "throttle: CPUQuota 200%, MemoryHigh pinned at current usage"
elif [ $new -lt 2 ] && [ $cur -ge 2 ]; then
  systemctl --user set-property --runtime $U CPUQuota=600% MemoryHigh=$(systemctl --user cat $U | awk -F= '/^MemoryHigh=/{print $2}' | tail -1); log "unthrottle: CPUQuota 600%, MemoryHigh restored"
fi
if [ $new -ge 3 ]; then  # shed the newest gate
  g=$(systemctl --user list-units --no-legend --plain 'fleet-gate-*.service' | awk '{print $1}' | while read u; do echo "$(systemctl --user show $u -p ActiveEnterTimestampMonotonic --value) $u"; done | sort -rn | head -1 | awk '{print $2}')
  if [ -n "$g" ]; then l=${g#fleet-gate-}; l=${l%.service}
    [ "$(systemctl --user show $U -p FreezerState --value)" = frozen ] && systemctl --user thaw $U
    systemctl --user stop $g && echo "$(date +%H:%M:%S) NEEDS-ESCALATION fail-closed: shed by fleet pressure guard (MemAvailable ${avail}G); re-gate later" >> ~/fleet/fb/lanes/$l.status && log "shed gate $l"; fi
fi
st=$(systemctl --user show $U -p FreezerState --value)
if [ $new -ge 4 ]; then [ "$st" = frozen ] || { systemctl --user freeze $U; log "EMERGENCY freeze"; }
elif [ "$st" = frozen ]; then systemctl --user thaw $U; log "thaw"; fi
[ "$new" != "$cur" ] && [ $new -lt 2 ] && [ $new -lt $cur ] && log "relaxed"
echo $new > $S/level
