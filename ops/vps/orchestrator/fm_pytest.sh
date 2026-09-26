#!/usr/bin/env bash
# fm_pytest.sh ROOT TEST_PATH... — run tests grouped by nearest pyproject.toml package dir; prints "FAILED <root-relative id>" lines; exit 0 ok, 1 test failures only, 2 infra/collection problem.
ROOT=$1; shift; declare -A grp; SEM=${FLEET_OPS_HOME:-$HOME/fleet/ops}/sem; mkdir -p $SEM
for t in "$@"; do d=$(dirname "$ROOT/$t"); while [ "$d" != "$ROOT" ] && [ ! -f "$d/pyproject.toml" ]; do d=$(dirname "$d"); done; grp[$d]+="${t#${d#$ROOT/}/} "; [ "$d" = "$ROOT" ] && grp[$d]+=""; done
worst=0
for d in "${!grp[@]}"; do
  rel=${d#$ROOT}; rel=${rel#/}
  ap=""; grep -q "^\[tool\.uv\.workspace\]" "$d/pyproject.toml" 2>/dev/null && ap="--all-packages"
  # memory budget (owner: <=75GB laptop): at most TEST_SLOTS concurrent pytest runs (each capped at 6G)
  exec 8>/dev/null; while :; do for i in $(seq 1 ${TEST_SLOTS:-10}); do exec 8>$SEM/test.$i; flock -n 8 && break 2; done; sleep 3; done
  out=$(cd "$d" && systemd-run --user --scope -q --slice=agents.slice -p MemoryMax=6G -p MemorySwapMax=0 timeout 1800 uv run $ap pytest -q -rfE -p no:cacheprovider ${grp[$d]} 2>&1); rc=$?
  exec 8>&-
  echo "$out" | grep -oE "^(FAILED|ERROR) [^ ]+" | awk -v p="${rel:+$rel/}" '{print "FAILED " p $2}'
  echo "SUMMARY ${rel:-.}: rc=$rc $(echo "$out" | grep -E "passed|failed|error" | tail -1 | cut -c1-120)"
  case $rc in 0) ;; 1) [ $worst -lt 1 ] && worst=1;; *) worst=2; echo "INFRA ${rel:-.}: $(echo "$out" | grep -E "Error|error" | head -2 | tr '\n' ' ' | cut -c1-200)";; esac
done
exit $worst
