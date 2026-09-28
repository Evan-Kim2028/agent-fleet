#!/bin/bash
# fleet-pressure-guard v4 (2026-09-27). Runs every 30 s (fleet-pressure-guard.timer).
# Graduated by MemAvailable, plus a host memory-stall shed that does not wait
# for free memory to disappear. The 2026-09-27 wedge sat at ~76% used and
# ~93% PSI full while MemAvailable stayed high enough that v3 never shed.
#   level 0 calm      : admission open, CPUQuota 600%, MemoryHigh as configured
#   level 1 mild      : avail < 12 or fleet PSI avg60 > 60 or host PSI avg60 > 95
#                       -> admission CLOSED (no new gates; running ones continue)
#   level 2 moderate  : avail <  8 -> CPUQuota 200%. No MemoryHigh pin.
#   level 3 severe    : avail <  4 -> stop the newest remote gate each tick
#   level 4 emergency : avail <  2 -> freeze fleet.slice
# Host stall (independent of those levels): memory PSI full avg60 > 60, or
# avg10 > 80, for two ticks in a row. Then close admission and stop the newest
# fleet gate and the newest Dagster run scope. One of each per tick. Do not
# freeze the slice. A pin or a freeze made this stall worse (2026-09-26).
# Relaxes one avail-level at a time after 5 calm minutes.
S=~/fleet/state; mkdir -p $S; U=fleet.slice
C=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/fleet.slice
# full line: "full avg10=.. avg60=.. avg300=.. total=.."
avg10=$(awk '/^full/{split($2,a,"="); print a[2]}' /proc/pressure/memory)
full=$(awk '/^full/{split($3,a,"="); print a[2]}' /proc/pressure/memory)
avail=$(awk '/^MemAvailable/{print int($2/1048576)}' /proc/meminfo)
gt(){ awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>b)}'; }
want=0
ffull=$(awk '/^full/{split($3,a,"="); print a[2]}' $C/memory.pressure 2>/dev/null || echo 0)
{ [ $avail -lt 12 ] || gt "$ffull" 60 || gt "$full" 95; } && want=1
[ $avail -lt 8 ] && want=2
[ $avail -lt 4 ] && want=3
[ $avail -lt 2 ] && want=4
cur=$(cat $S/level 2>/dev/null || echo 0)
if [ $want -ge $cur ]; then new=$want; rm -f $S/calm_since
else
  [ -f $S/calm_since ] || date +%s > $S/calm_since
  if [ $(( $(date +%s) - $(cat $S/calm_since) )) -ge 300 ]; then new=$((cur-1)); date +%s > $S/calm_since; else new=$cur; fi
fi
log(){ logger -t fleet-guard "level $cur->$new (avail=${avail}G psi60=$full psi10=$avg10): $*"; }
[ $new -ge 1 ] && echo closed > $S/admission || echo open > $S/admission
if [ $new -ge 2 ] && [ $cur -lt 2 ]; then
  systemctl --user set-property --runtime $U CPUQuota=200%; log "throttle: CPUQuota 200%"
elif [ $new -lt 2 ] && [ $cur -ge 2 ]; then
  systemctl --user set-property --runtime $U CPUQuota=600%; log "unthrottle: CPUQuota 600%"
fi
newest_unit(){
  systemctl --user list-units --full --no-legend --plain --no-pager "$1" 2>/dev/null | awk '{print $1}' | while read -r u; do
    [ -n "$u" ] || continue
    echo "$(systemctl --user show "$u" -p ActiveEnterTimestampMonotonic --value) $u"
  done | sort -rn | head -1 | awk '{print $2}'
}
shed_gate(){
  g=$(newest_unit 'fleet-gate-*.service')
  [ -n "$g" ] || return 0
  l=${g#fleet-gate-}; l=${l%.service}
  [ "$(systemctl --user show $U -p FreezerState --value)" = frozen ] && systemctl --user thaw $U
  systemctl --user stop "$g" && echo "$(date +%H:%M:%S) NEEDS-ESCALATION fail-closed: shed by fleet pressure guard (avail ${avail}G psi60=$full); re-gate later" >> ~/fleet/fb/lanes/$l.status && log "shed gate $l"
}
if [ $new -ge 3 ]; then
  shed_gate
fi
# Dagster-run yield (2026-09-28): a heavy Dagster run worker is bounded-time
# prod work holding ~10-32G inside lor-dagster-run-heavy.slice; fleet work is
# delay-tolerant. Freeze fleet.slice while a heavy run is active so gates
# never compete with a run for the same reclaim path. The cgroup freezer
# keeps the gate's findings/worktree; a mid-verify shed re-runs lens find.
DGRH=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/lor.slice/lor-dagster.slice/lor-dagster-run.slice/lor-dagster-run-heavy.slice/memory.current
dgr=$(cat $DGRH 2>/dev/null || echo 0)
st=$(systemctl --user show $U -p FreezerState --value)
want_frozen=0
[ $new -ge 4 ] && want_frozen=1
[ "${dgr:-0}" -gt 104857600 ] && want_frozen=1
if [ $want_frozen = 1 ] && [ "$st" != frozen ]; then
  systemctl --user freeze $U; log "freeze (level=$new dagster_heavy_bytes=${dgr:-0})"
elif [ $want_frozen = 0 ] && [ "$st" = frozen ]; then
  systemctl --user thaw $U; log "thaw"
fi
[ "$new" != "$cur" ] && [ $new -lt 2 ] && [ $new -lt $cur ] && log "relaxed"
echo $new > $S/level

# Host memory stall. avg60 > 60 means the last minute was mostly stuck.
# avg10 > 80 catches a stall that is already severe before the minute average catches up.
# Two ticks (about a minute of confirmation) so one sample does not kill a job.
stall=0
{ gt "$full" 60 || gt "$avg10" 80; } && stall=1
if [ "$stall" -eq 1 ]; then
  n=$(cat $S/stall_ticks 2>/dev/null || echo 0)
  n=$((n+1))
else
  n=0
fi
echo $n > $S/stall_ticks
# avg60 > 60 is already a full minute of stall. Shed on that tick.
# A 10-second spike has to repeat once before it counts.
if { [ "$stall" -eq 1 ] && gt "$full" 60; } || [ "$n" -ge 2 ]; then
  echo closed > $S/admission
  shed_gate
  # Review gates and vision stop first. The single dbt job is the work
  # that has to finish. Stop it only when no gate is left and the stall remains.
  gleft=$(newest_unit 'fleet-gate-*.service')
  # 60 sheds review gates. A single dbt job sits near that while it spills.
  # Kill it only in the range that locked SSH out last time, about 80 and up.
  if [ -z "$gleft" ] && { gt "$full" 80 || gt "$avg10" 90; }; then
    d=$(newest_unit 'lor-dagster-run-*.scope')
    if [ -n "$d" ]; then
      systemctl --user stop "$d" && log "shed dagster $d (host psi60=$full psi10=$avg10 ticks=$n)"
    fi
  fi
  v=$(newest_unit "vision-*.service")
  [ -n "$v" ] || v=$(newest_unit "ebay-image-analysis.service")
  if [ -n "$v" ]; then
    systemctl --user stop "$v" && log "shed $v (host psi60=$full)"
  fi
  # The box has five schedulers that do not read this guard: the hourly health
  # sweep, table maintenance, lakestore maintenance, vision runs, and anything
  # an ssh session left running. They are what pushed load to 67 with 40 GB
  # free. Shed the newest of those, newest first, but never the API, storage,
  # the catalog, or the dagster daemon.
  for pat in 'lake-run-*.scope' 'lake-health-sweep.service' 'pokemon-lor-table-maintenance@*.service'; do
    u=$(newest_unit "$pat")
    [ -n "$u" ] || continue
    systemctl --user stop "$u" && log "shed $u (host psi60=$full)"
    break
  done
  log "host stall: admission closed, shed one gate, one dagster scope, one vision unit"
fi

# janitor: every ~10 min, remove idle agent scratch dirs from /tmp (tmpfs = RAM)
[ $(( $(date +%s) / 30 % 20 )) -eq 0 ] && ~/fleet/bin/tmp_janitor.sh >/dev/null 2>&1

# Stopgap until agent-fleet #133 (memcap scopes join the caller slice) is installed: fleet lane agents spawned by
# `fleet lane run` land in app.slice (4G MemoryHigh envelope) and get throttled for hours. Move every descendant of a
# fleet-lane-0e-* unit that sits in app.slice back into that unit's own cgroup (owner 2026-09-27).
FSL=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/fleet.slice
_desc(){ echo $1; for _c in $(pgrep -P $1); do _desc $_c; done; }
for _u in $(systemctl --user list-units 'fleet-lane-0e-*' --state=active --no-legend 2>/dev/null | awk '{print $1}'); do
  _m=$(systemctl --user show -p MainPID --value $_u); [ "${_m:-0}" -gt 0 ] || continue
  for _p in $(_desc $_m); do grep -q '/app.slice/' /proc/$_p/cgroup 2>/dev/null && echo $_p > $FSL/$_u/cgroup.procs 2>/dev/null; done
done
