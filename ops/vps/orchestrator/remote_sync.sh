#!/usr/bin/env bash
# remote_sync.sh — every 30 s, copy new lines from lor-main ~/fleet/fb/lanes/*.status into $F/lanes/LANE.status and
# $F/events.log ("[LANE] gate(remote): ..."), and refresh $F/remote_live.txt (lanes whose fleet-gate-LANE service is
# active on lor-main). Byte offsets live in $F/remote_offsets.json, so a restart never duplicates lines.
F=${FLEET_OPS_HOME:-$HOME/fleet/ops}
OFF=$F/remote_offsets.json; [ -s $OFF ] || echo '{}' > $OFF
while :; do
  dump=$(timeout 60 ssh -o ConnectTimeout=20 ${FLEET_VPS_HOST:-lake-vps-lor-main} 'python3 ~/fleet/fb/sync_dump.py' < $OFF 2>/dev/null)
  [ -n "$dump" ] && printf '%s' "$dump" | python3 $F/remote_sync_apply.py $F $OFF
  sleep 30
done
