#!/usr/bin/env bash
set -uo pipefail
F=$HOME/fleet/fb
S=$HOME/fleet/state
Q=$F/gate_queue.txt
DONE=$F/gate_queue_done.txt
RQ=$F/.requeue_counts
QFILE=$F/quarantine.txt
PRCACHE=$F/.pr_cache
MAXG=${MAX_GATES:-3}
CAPACITY=$HOME/fleet/bin/fleet_admission.py
mkdir -p "$F/locks"
touch "$RQ" "$QFILE" "$DONE"
exec 9>"$F/.gate_queue.lock"
flock -n 9 || exit 0

log(){ logger -t gate-queue "$*"; printf '%s %s\n' "$(date +%T)" "$*" >> "$F/gate_queue.log"; }
gate_units(){ systemctl --user list-units --no-legend --plain 'fleet-gate-*.service' --state=running,activating 2>/dev/null | awk '{print $1}' | grep -v fleet-gate-queue.service; }
gate_processes(){
  ps -eo args= | awk '{for(i=1;i<NF;i++){t=$i; sub(/^.*\//,"",t); if(t=="fbgate") print $(i+1)}}' | sort -u
}
live_gates(){
  { gate_units | sed -E 's/^fleet-gate-//; s/\.service$//'; gate_processes; } | grep -v '^$' | sort -u
}
queued(){ awk 'NF >= 3 && $1 !~ /^#/{print $1}' "$Q" 2>/dev/null; }
requeues(){ awk -v l="$1" -v h="$2" '$1==l && $4==h && $3!="rebase"{c++} END{print c+0}' "$RQ" 2>/dev/null; }
attempts(){ awk -v l="$1" -v h="$2" '$1==l && $4==h{c++} END{print c+0}' "$DONE" 2>/dev/null; }
rebase_tries(){ awk -v l="$1" -v h="$2" '$1==l && $3=="rebase" && $4==h{c++} END{print c+0}' "$RQ" 2>/dev/null; }
is_quarantined(){ awk -v l="$1" -v h="$2" '$1==l && $2==h{found=1} END{exit !found}' "$QFILE" 2>/dev/null; }
quarantine(){
  local lane=$1 sha=$2 reason=$3
  is_quarantined "$lane" "$sha" || { printf '%s %s %s %s\n' "$lane" "$sha" "$(date +%s)" "$reason" >> "$QFILE"; log "QUARANTINE $lane@$sha ($reason)"; }
}
refresh_prs(){
  local tmp ok=1 repo
  tmp=$(mktemp "$F/.pr_cache.XXXXXX") || return 1
  for repo in lake-of-rage silphcoanalytics agent-fleet; do
    timeout 30 gh pr list -R "Evan-Kim2028/$repo" --limit 100 --state open \
      --json number,headRefName,headRefOid \
      --jq ".[] | \"$repo \" + (.number|tostring) + \" \" + .headRefName + \" \" + .headRefOid" \
      >> "$tmp" 2>/dev/null || ok=0
  done
  if [ "$ok" -eq 1 ]; then mv "$tmp" "$PRCACHE"; else rm -f "$tmp"; [ -f "$PRCACHE" ] || : > "$PRCACHE"; fi
}
find_pr(){
  awk -v fb="fb/$1" -v dq="dq1d/${1#dq1d-}" '$3==fb || $3==dq{print $1, $2, $4; exit}' "$PRCACHE" 2>/dev/null
}
pr_head(){ awk -v r="$1" -v p="$2" '$1==r && $2==p{print $4; exit}' "$PRCACHE" 2>/dev/null; }
remove_queue(){
  local lane=$1 repo=$2 pr=$3 tmp
  tmp=$(mktemp "$F/.gate_queue.XXXXXX") || return 1
  awk -v l="$lane" -v r="$repo" -v p="$pr" '!(NF>=3 && $1==l && $2==r && $3==p)' "$Q" > "$tmp"
  mv "$tmp" "$Q"
}
write_brief(){
  local lane=$1 gate=$F/gate/$1 prompt=$F/prompts/$1.task.md
  [ -f "$prompt" ] && return 0
  [ -s "$gate/candidates.jsonl" ] || return 0
  {
    printf '# Re-gate briefing for %s\n' "$lane"
    printf 'The previous gate failed. Start from these prior findings and fix the confirmed issue.\n\n'
    printf '## Candidate blockers\n'
    head -20 "$gate/candidates.jsonl" | cut -c1-400
    [ -s "$gate/confirmed.jsonl" ] && { printf '\n## Confirmed claims\n'; head -15 "$gate/confirmed.jsonl" | cut -c1-400; }
    [ -s "$gate/confirmed_untestable.jsonl" ] && { printf '\n## Untestable claims\n'; head -10 "$gate/confirmed_untestable.jsonl" | cut -c1-400; }
    [ -s "$gate/rounds.tsv" ] && { printf '\n## Fixer history\n'; tail -10 "$gate/rounds.tsv" | cut -c1-300; }
    [ -s "$gate/judge1.md" ] && { printf '\n## Judge rationale\n'; tail -30 "$gate/judge1.md" | cut -c1-400; }
  } > "$prompt"
  log "wrote informed brief $prompt"
}
scan_escalations(){
  local lane last cls n repo pr sha age
  local gates queued_lanes rebasing
  refresh_prs || return 0
  gates=$(gate_units)
  queued_lanes=$(queued)
  rebasing=$(systemctl --user list-units --no-legend --plain 'fleet-rebase-*.service' --state=running,activating 2>/dev/null | awk '{print $1}')
  for status in "$F"/lanes/*.status; do
    [ -f "$status" ] || continue
    lane=$(basename "$status" .status)
    grep -qx "fleet-gate-$lane.service" <<< "$gates" && continue
    last=$(tail -1 "$status" 2>/dev/null)
    if grep -qx "$lane" <<< "$queued_lanes"; then
      case "$last" in
        *'merge conflict'*)
          read -r queued_repo queued_pr < <(awk -v l="$lane" '$1==l{print $2, $3; exit}' "$Q")
          [ -n "${queued_pr:-}" ] && remove_queue "$lane" "$queued_repo" "$queued_pr"
          log "reroute queued merge-conflict $lane to rebase"
          ;;
        *) continue;;
      esac
    fi
    cls=
    case "$last" in
      *NEEDS-REBASE*|*'NEEDS-ESCALATION rebase:'*|*'rebase agent starting'*|*'merge conflict'*|*'conflict with main'*) cls=rebase;;
      *'start @'*)
        age=$(( $(date +%s) - $(stat -c %Y "$status" 2>/dev/null || echo 0) ))
        [ "$age" -ge 1800 ] && cls=infra || continue;;
      *'PREMERGE-APPROVED'*) continue;;
      *'shed by fleet pressure'*|*fail-closed*|*died*|*'could not run'*|*'gate refused'*|*'gate stuck'*) cls=infra;;
      *no-push*|*'stalled after'*|*'merged-tree regression'*|*untestable*|*'fix round pushed nothing'*) cls=rework;;
      *) continue;;
    esac
    repo= pr= sha=
    read -r repo pr sha < <(find_pr "$lane")
    [ -n "${sha:-}" ] || continue
    is_quarantined "$lane" "$sha" && continue
    if [ "$cls" = rebase ]; then
      grep -qx "fleet-rebase-$lane.service" <<< "$rebasing" && continue
      [ -z "$rebasing" ] || continue
      n=$(rebase_tries "$lane" "$sha")
      if [ "$n" -ge 2 ]; then quarantine "$lane" "$sha" 'rebase attempts exhausted'; continue; fi
      printf '%s %s rebase %s\n' "$lane" "$(date +%s)" "$sha" >> "$RQ"
      if systemd-run --user --quiet --collect --unit="fleet-rebase-$lane" --slice=fleet.slice \
        "$HOME/fleet/bin/lane_rebase.sh" "$lane" "$repo" "$pr"; then
        rebasing="${rebasing}"$'\n'"fleet-rebase-$lane.service"
        log "rebase $lane ($repo#$pr) spawned ($n prior for $sha)"
      else
        log "rebase spawn failed $lane"
      fi
      continue
    fi
    lim=3; [ "$cls" = rework ] && lim=1
    n=$(requeues "$lane" "$sha")
    if [ "$n" -ge "$lim" ]; then quarantine "$lane" "$sha" "$cls attempts exhausted"; continue; fi
    n=$(attempts "$lane" "$sha")
    if [ "$n" -ge 6 ]; then quarantine "$lane" "$sha" 'attempt cap'; continue; fi
    [ "$cls" = rework ] && write_brief "$lane"
    printf '%s %s %s\n' "$lane" "$repo" "$pr" >> "$Q"
    printf '%s %s %s %s\n' "$lane" "$(date +%s)" "$cls" "$sha" >> "$RQ"
    log "re-queue $lane ($repo#$pr) class=$cls ($((n)) prior for $sha)"
  done
}

while :; do
  scan_escalations
  line=$(awk 'NF>=3 && $1 !~ /^#/{print $1, $2, $3; exit}' "$Q" 2>/dev/null)
  if [ -z "$line" ]; then sleep 90; continue; fi
  read -r lane repo pr <<< "$line"
  sha=$(pr_head "$repo" "$pr")
  if [ -z "$sha" ]; then
    remove_queue "$lane" "$repo" "$pr"
    log "dropped stale queue entry $lane ($repo#$pr is not open)"
    continue
  fi
  if is_quarantined "$lane" "$sha"; then
    remove_queue "$lane" "$repo" "$pr"
    continue
  fi
  n=$(live_gates | grep -c . || true)
  if [ "$n" -ge "$MAXG" ] || ! python3 "$CAPACITY" --check; then sleep 90; continue; fi
  if [ "$(attempts "$lane" "$sha")" -ge 6 ]; then
    quarantine "$lane" "$sha" 'attempt cap'
    remove_queue "$lane" "$repo" "$pr"
    continue
  fi
  systemctl --user reset-failed "fleet-gate-$lane.service" 2>/dev/null
  if systemd-run --user --quiet --collect --unit="fleet-gate-$lane" --slice=fleet.slice \
    -p RuntimeMaxSec=21600 \
    -p "StandardOutput=append:$F/gate-$lane.log" -p "StandardError=append:$F/gate-$lane.log" \
    /bin/bash -c "~/fleet/fb/fbgate $lane $repo $pr"; then
    remove_queue "$lane" "$repo" "$pr"
    printf '%s %s %s %s %s\n' "$lane" "$repo" "$pr" "$sha" "$(date +%s)" >> "$DONE"
    log "fired gate $lane ($repo#$pr @$sha) [$n in flight]"
  else
    log "fire failed $lane; retrying next tick"
  fi
  sleep 90
done
