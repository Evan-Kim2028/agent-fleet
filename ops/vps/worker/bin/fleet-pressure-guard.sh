#!/bin/bash
# fleet-pressure-guard. Runs every 30 s. Admission uses measured host and fleet cgroup headroom.
# PSI remains a pressure signal, but cannot close gate admission or shed work by itself.
# The fleet is bounded by its own 12G/15G envelope; production units are not stopped here.
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
# admission now decided at end-of-tick (capacity rule below)
if [ $new -ge 2 ] && [ $cur -lt 2 ]; then
  systemctl --user set-property --runtime $U CPUQuota=200%; log "throttle: CPUQuota 200%"
elif [ $new -lt 2 ] && [ $cur -ge 2 ]; then
  systemctl --user set-property --runtime $U CPUQuota=600%; log "unthrottle: CPUQuota 600%"
fi
# fleet-gate-queue.service is the gate DRIVER, not a gate — exclude it
# from every glob so a stall-shed never kills the driver (2026-09-28).
newest_unit(){
  systemctl --user list-units --full --no-legend --plain --no-pager "$1" 2>/dev/null | awk '{print $1}' | grep -v fleet-gate-queue.service | while read -r u; do
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
# Freeze only at the emergency host-memory level.
st=$(systemctl --user show $U -p FreezerState --value)
want_frozen=0
[ $new -ge 4 ] && want_frozen=1
if [ $want_frozen = 1 ] && [ "$st" != frozen ]; then
  systemctl --user freeze $U; log "freeze (level=$new)"
elif [ $want_frozen = 0 ] && [ "$st" = frozen ]; then
  systemctl --user thaw $U; log "thaw"
fi
[ "$new" != "$cur" ] && [ $new -lt 2 ] && [ $new -lt $cur ] && log "relaxed"
echo $new > $S/level

# Host PSI is diagnostic; actions below also require real host or fleet scarcity.
stall=0
{ gt "$full" 60 || gt "$avg10" 80; } && stall=1
if [ "$stall" -eq 1 ]; then
  n=$(cat $S/stall_ticks 2>/dev/null || echo 0)
  n=$((n+1))
else
  n=0
fi
echo $n > $S/stall_ticks
# Reclaim PSI alone is not grounds to stop a gate or production unit.
if { [ "$stall" -eq 1 ] && gt "$full" 60; } || [ "$n" -ge 2 ]; then
  fleet_current=$(cat "$C/memory.current" 2>/dev/null || echo 0)
  fleet_limit=$(cat "$C/memory.max" 2>/dev/null || echo max)
  if [ "$avail" -lt 8 ] || { [ "$fleet_limit" != max ] && [ $((fleet_limit-fleet_current)) -lt 1073741824 ]; }; then
    shed_gate
  fi
  log "memory PSI high; capacity admission remains authoritative (avail=${avail}G fleet=$fleet_current/$fleet_limit)"
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

python3 "$HOME/fleet/bin/fleet_admission.py" --fleet-cgroup "$C" --state "$S/admission" || :
