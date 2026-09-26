#!/bin/bash
# dbt_parse_check.sh SHA — `dbt parse --target prod` of (origin/main + SHA) in a scratch worktree on lor-main.
# transform-ci.yml never runs (Actions billing), so this is the only parse check before a lake deploy.
# Exit 0 parse ok, 1 parse failed, 3 merge conflict, 2 infra.
SHA=$1; [[ "$SHA" =~ ^[0-9a-f]{7,40}$ ]] || { echo "usage: dbt_parse_check.sh SHA" >&2; exit 64; }
VPS_HOST=${FLEET_VPS_HOST:-lake-vps-lor-main}
ssh -o ConnectTimeout=20 $VPS_HOST "~/bin/vps-run light -- bash -s -- $SHA" <<'REMOTE'
SHA=$1; R=~/lake-of-rage; S=$(mktemp -d /tmp/dbtparse.XXXX)
cleanup(){ git -C $R worktree remove --force $S/wt >/dev/null 2>&1; git -C $R worktree prune; rm -rf $S; }; trap cleanup EXIT
git -C $R fetch -q origin || exit 2
git -C $R worktree add -q --detach $S/wt origin/main || exit 2
cd $S/wt && git -c user.name=parse -c user.email=parse@local merge -q --no-ff --no-commit $SHA >/dev/null 2>&1 || { echo "PARSE-CHECK conflict merging $SHA into origin/main"; exit 3; }
cp $R/transform/profiles.yml transform/
set -a; [ -f $R/pipelines/pokemontcg_pipe/.env ] && . $R/pipelines/pokemontcg_pipe/.env; set +a
export DBT_DUCKDB_PATH=$S/parse.duckdb
cd transform && timeout 900 $R/transform/.venv/bin/dbt parse --profiles-dir . --target prod --target-path $S/target > $S/parse.log 2>&1; rc=$?
if [ $rc = 0 ]; then echo "PARSE-CHECK ok (origin/main + ${SHA:0:9})"; exit 0; fi
echo "PARSE-CHECK FAILED (origin/main + ${SHA:0:9}):"; grep -iE "error|nested|Compilation|did not" $S/parse.log | head -8; exit 1
REMOTE
