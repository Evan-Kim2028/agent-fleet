#!/bin/bash
# wait_net.sh — network backpressure before starting an agent: GitHub DNS+connect must be <3s twice in a row.
# The home uplink saturates when dozens of agents upload context at once (router RTT 214 ms, DNS 1-4 s,
# agents dying "Unable to connect", 2026-09-26 00:45). Waits at most 15 min, then lets the agent start anyway.
ok=0; start=$(date +%s); logged=0
while :; do
  # time_connect includes the DNS lookup: the ISP resolvers took 20+ s at times (2026-09-26 01:1x).
  # cmd agents talk to api.commandcode.ai, pushes/PRs to api.github.com; both must be healthy.
  t=$(curl -s -o /dev/null -w "%{time_connect}" --max-time 8 https://api.github.com 2>/dev/null)
  t2=$(curl -s -o /dev/null -w "%{time_connect}" --max-time 8 https://api.commandcode.ai 2>/dev/null)
  if awk -v t="${t:-0}" -v u="${t2:-0}" 'BEGIN{exit !(t>0 && t<3 && u>0 && u<3)}'; then ok=$((ok+1)); else ok=0; fi
  # A 200 from the API root does not prove model calls work (seen 2026-09-26): once the link looks healthy, confirm
  # with a real one-turn model call, cached for 90 s across all agent launches.
  if [ $ok -ge 2 ]; then
    C=${FLEET_OPS_HOME:-$HOME/fleet/ops}/.cmd_ok
    if [ -z "$(find $C -newermt '-90 seconds' 2>/dev/null)" ]; then
      if echo "Reply with the single word OK." | timeout 60 cmd -p -m "${FB_MODEL:-stealth/space-bunny-alpha}" --skip-onboarding --no-auto-update --output-format json --max-turns 1 >/dev/null 2>&1; then touch $C; else ok=0; fi
    fi
  fi
  [ $ok -ge 2 ] && exit 0
  [ $(( $(date +%s) - start )) -ge 900 ] && { echo "wait_net: link still congested after 15 min; starting anyway" >&2; exit 0; }
  [ $logged = 0 ] && { echo "$(date +%H:%M:%S) [net] congested (github connect=${t:-timeout}s); holding agent starts" >> ${FLEET_OPS_HOME:-$HOME/fleet/ops}/events.log; logged=1; }
  sleep $((5 + RANDOM % 10))
done
