#!/usr/bin/env bash
source ~/fleet/env.sh
# fm_pytest.sh ROOT TEST_PATH... — run tests grouped by nearest pyproject.toml package dir; prints "FAILED <root-relative id>" lines; exit 0 ok, 1 test failures only, 2 infra/collection problem.
ROOT=$1; shift; declare -A grp; SEM=${AGENT_FLEET_HOME:-$HOME/.agent-fleet}/slots/test; CAPACITY=${FLEET_CAPACITY_HELPER:-$HOME/fleet/bin/fleet_admission.py}; mkdir -p "$SEM"
# Result cache (owner 2026-09-26: batch/avoid repeated test runs). Key = git tree of the WHOLE worktree including
# uncommitted and untracked files (temp index) + the test list; any code change gives a new key. TTL 24 h.
TC=$HOME/fleet/cache/fmtest; mkdir -p $TC; find $TC -maxdepth 1 -type f -mmin +1440 -delete 2>/dev/null
_ix=$(mktemp); cp "$(git -C "$ROOT" rev-parse --git-path index 2>/dev/null | sed "s#^[^/]#$ROOT/&#")" $_ix 2>/dev/null
_tree=$(cd "$ROOT" && GIT_INDEX_FILE=$_ix git add -A . >/dev/null 2>&1 && GIT_INDEX_FILE=$_ix git write-tree 2>/dev/null); rm -f $_ix
_key=$(printf '%s %s' "$_tree" "$(printf '%s\n' "$@" | sort | tr '\n' ' ')" | sha256sum | cut -c1-40)
if [ -n "$_tree" ] && [ -f $TC/$_key.out ] && [ -f $TC/$_key.rc ]; then cat $TC/$_key.out; echo "CACHE hit $_key" >&2; exit $(cat $TC/$_key.rc); fi
exec 3>&1; _cache_out=$(mktemp)
trap '_rc=$?; [ -n "$_tree" ] && [ $_rc -lt 2 ] && { cp $_cache_out $TC/$_key.out; echo $_rc > $TC/$_key.rc; }; rm -f $_cache_out' EXIT
exec > >(tee $_cache_out >&3)
for t in "$@"; do d=$(dirname "$ROOT/$t"); while [ "$d" != "$ROOT" ] && [ ! -f "$d/pyproject.toml" ]; do d=$(dirname "$d"); done; grp[$d]+="${t#${d#$ROOT/}/} "; [ "$d" = "$ROOT" ] && grp[$d]+=""; done
worst=0
for d in "${!grp[@]}"; do
  rel=${d#$ROOT}; rel=${rel#/}
  ap=""; grep -q "^\[tool\.uv\.workspace\]" "$d/pyproject.toml" 2>/dev/null && ap="--all-packages"
  # Machine-wide test admission: one pytest run across gate, lane, and direct-agent paths.
  exec 8>/dev/null; test_waited=0
  while :; do
    for i in $(seq 0 $((${TEST_SLOTS:-1} - 1))); do
      exec 8>"$SEM/slot.$i"
      if flock -n 8; then
        if [ ! -x "$CAPACITY" ] || python3 "$CAPACITY" --check --fleet-headroom-gib 6; then break 2; fi
        exec 8>&-
      fi
    done
    sleep 3; test_waited=$((test_waited+3))
    [ "$test_waited" -lt "${TEST_ADMISSION_WAIT_S:-3600}" ] || { echo "INFRA: no fleet headroom for pytest after ${test_waited}s"; exit 2; }
  done
  out=$(cd "$d" && systemd-run --user --scope -q --slice=fleet.slice -p MemoryMax=6G -p MemorySwapMax=0 env PYTHONPATH="$d:$ROOT${PYTHONPATH:+:$PYTHONPATH}" timeout 1800 uv run $ap python -m pytest -q -rfE -p no:cacheprovider ${grp[$d]} 2>&1); rc=$?
  if [ $rc -ge 1 ] && grep -qE 'Failed to spawn: `pytest`|No module named pytest' <<< "$out"; then
    # a package dir whose env has no pytest (e.g. the silph repo root that an importer search reached) failed 4 of 10
    # merged-tree checks at 11:00-15:00 as "could not run": run it with pytest added instead of failing the gate
    out=$(cd "$d" && systemd-run --user --scope -q --slice=fleet.slice -p MemoryMax=6G -p MemorySwapMax=0 env PYTHONPATH="$d:$ROOT${PYTHONPATH:+:$PYTHONPATH}" timeout 1800 uv run $ap --with pytest python -m pytest -q -rfE -p no:cacheprovider ${grp[$d]} 2>&1); rc=$?
  fi
  exec 8>&-
  echo "$out" | grep -oE "^(FAILED|ERROR) [^ ]+" | awk -v p="${rel:+$rel/}" '{print "FAILED " p $2}'
  echo "SUMMARY ${rel:-.}: rc=$rc $(echo "$out" | grep -E "passed|failed|error" | tail -1 | cut -c1-120)"
  case $rc in 0) ;; 1) [ $worst -lt 1 ] && worst=1;; *) worst=2; echo "INFRA ${rel:-.}: $(echo "$out" | grep -E "Error|error" | head -2 | tr '\n' ' ' | cut -c1-200)";; esac
done
exit $worst
