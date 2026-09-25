# merge-plan — which approved PRs ship together

`agent-fleet merge-plan` is the command center's answer to one question:
**given everything the gate has approved, what is the smallest number of
merges and deploys that ships all of it?**

Merging and deploying is the slowest serialized step in the fleet's loop. A
lake-of-rage deploy is roughly 15-20 minutes; a silphcoanalytics deploy about
9. An approved PR that sits waiting for its own deploy is pure latency, so
the planner groups PRs so that one deploy plus one verify covers as many of
them as it safely can.

```
agent-fleet merge-plan --repo-path ~/Documents/lake-of-rage
agent-fleet merge-plan --repo-path ~/Documents/lake-of-rage \
                       --repo-path ~/Documents/silphcoanalytics --json
agent-fleet merge-plan --repo-path ~/Documents/lake-of-rage --operator op
agent-fleet merge-plan --repo-path ~/Documents/lake-of-rage --emit
```

| Flag | Meaning |
|---|---|
| `--repo-path PATH` | Checkout to plan for. Repeatable. Overrides `merge_plan.repos[]`. |
| `--operator X` | Only read lanes for operator `X`. Default: all operators. |
| `--status-dir DIR` | Also read gate status files containing `PREMERGE-APPROVED <sha>`. |
| `--max-batch-size N` | Cap on PRs per batch. Default 5. |
| `--no-merge-check` | Skip the git merge-compatibility check (file-overlap rules still apply). |
| `--emit` | Emit the plan as a `merge.plan` event for the dashboard. |
| `--json` | Emit the plan as JSON. |

## Where approvals come from

A PR is eligible only when the gate has approved it, recorded as a
`PREMERGE-APPROVED <sha9>` status line in either of two places:

* the **lane registry** — `~/.agent-fleet/lanes/<operator>/<lane>.json`, field
  `status_line`;
* a **status directory** (`--status-dir`) of files with the same marker. A
  status file may carry `owner/repo#123` anywhere in its text so one directory
  can cover several repositories.

Both sources are merged and de-duplicated by `(repo, pr_number)`.

### Stale approvals

An approval names a specific commit. If the PR head has moved since, the gate
never reviewed the new commits, so `merge-plan` reports it and **excludes it
from batching**:

```
excluded #101 lake-of-rage: stale approval: gate approved 947789269, head is now 0d8eb63a6
```

This is normal operational state, not an error — the plan still renders and
still batches everything else. If the PR head cannot be read at all (a `gh`
failure), that is reported separately rather than assumed to be approved.

## The change profile

For each approved PR, `gh pr view` gives the changed files against the PR's
own base. From those paths alone `merge-plan` derives:

**Deploy unit** — which single deploy would cover the change. A batch holds
exactly one unit, so this is what makes "one deploy, one verify" true.

| Repo | Units |
|---|---|
| `lake-of-rage` | `lor-api`, `pipelines`, `dbt`, `orchestration` |
| `silphcoanalytics` | `api`, `frontend`, `mobile`, `pipeline` |
| `agent-fleet` | `package` |

A PR touching two units takes the heavier one, so a change that also rebuilds
the API ships as an API batch. The tables are overridable per repo in
`fleet.yaml`.

**dbt models** — `transform/models/**/*.{sql,py,yml}` parses to model names
(`transform/models/gold/sales.sql` → `gold.sales`). When
`transform/target/manifest.json` is present, the set is expanded to the full
downstream closure by walking the manifest's `parent_map`, so the rebuild
covers everything the change can invalidate. Without a manifest only the
directly edited models are selected, and the plan records
`dbt_select_source: direct` so the operator knows the difference.

**Risk flags** — `migration` (sql, `gold_catalog.json`, `packages/lakestore`,
alembic), `deploy` (workflow files, `infra/vps`, deploy scripts, Dockerfiles),
`prod_write` (`pipe/ops/*`, `pipelines/*`, `ops/*`, `scripts/*`).

**Size** — additions plus deletions from the diff.

## The batching algorithm

Deterministic and documented. Five rules, applied in this order:

1. **One deploy unit per batch.** Every PR in a batch shares a unit, so one
   deploy and one verify covers the whole batch.

2. **No file overlap.** Two PRs touching the same file never share a batch.
   PRs are walked in ascending PR number, so on a conflict the lower number
   lands first — oldest work first, which is both a stable repo fact and the
   order an operator would expect.

3. **dbt models rebuild once.** PRs whose `--select` sets intersect are unioned
   into one group (via connected components) *before* batching, so their
   downstream dbt/Dagster rebuild runs a single time instead of once per PR.
   This is why two unrelated-looking PRs can end up in the same batch.

4. **Risky PRs are alone, and ship last.** A migration, a deploy script, or a
   prod-write tool is never batched with anything else: when it breaks, the
   blast radius is exactly that one PR. These batches go **last** so that every
   safe batch has already landed and verified, and a failing risky deploy rolls
   back with no unverified changes riding along.

5. **Size cap.** Batches are capped (default 5).

### When the cap and dbt grouping disagree

The cap is a hard safety bound; dbt grouping is best-effort *within* it. A dbt
group larger than the cap splits into consecutive sub-batches, each with its
own `--select` set, and the split is labelled in the output so the operator
knows the rebuild will run more than once:

```
- dbt group of 7 PRs split by the 5-PR cap; the rebuild runs once per sub-batch
```

### Merge compatibility

A batch also has to be *landable*. `merge-plan` verifies that with
`git merge-tree --write-tree` on git >= 2.38, folding each merge onto the
previous result. On older git (this box runs 2.34) it falls back to a scratch
worktree under a temp directory, replaying the merges with
`git merge --no-commit`. The worktree is created and removed by the check
itself; your repo is never mutated. If a batch still cannot merge cleanly, it
is peeled back to single-PR batches rather than shipping an unlandable merge.

File-overlap analysis already proves most batches disjoint, and
`--no-merge-check` skips the git work entirely when that is enough.

### Determinism

Every ordering is by `(repo, pr_number)` — values that do not change between
runs over the same input. No clock and no filesystem iteration order reaches
the output, so identical input yields byte-identical plan JSON. There is a
test that asserts exactly this.

## Output

```
batch 0: lake-of-rage [dbt] #12@1a2b3c4d, #14@5e6f7a8b
  dbt --select gold.cardindex gold.sales
  $ lake_batch_merge.sh 12:1a2b3c4d 14:5e6f7a8b
  - 2 PR(s) share deploy unit 'dbt' — one deploy + verify covers the batch
  - dbt --select rebuild runs once for: gold.cardindex, gold.sales
```

`--json` emits the same thing structurally, with `batches[].prs`,
`batches[].reasons`, `batches[].dbt_select`, `batches[].executor_commands`,
and an `excluded` list carrying the stale and unreadable PRs with reasons.

## Executor templates

The command lines an operator runs are **config-driven and have no built-in
defaults**. The two operator sessions merge with different scripts that take
different argument shapes, and `merge-plan` will not print a plausible-looking
command that does not exist on the box. With no template configured the plan
says so explicitly:

```
(no executor template configured for this repo)
note: no executor template configured for: lake-of-rage
```

Configure them in `~/.agent-fleet/fleet.yaml`:

```yaml
merge_plan:
  repos:
    - name: lake-of-rage
      path: ~/Documents/lake-of-rage
      # One command for the whole batch; {pr_args} expands to "12:1a2b3c4 14:5e6f7a8".
      merge_template: "scripts/lake_batch_merge.sh {pr_args}"
    - name: silphcoanalytics
      path: ~/Documents/silphcoanalytics
      # One command per PR, chained in batch order; {pr} and {sha9} are substituted.
      merge_per_pr_template: "scripts/silph_merge_verify.sh {pr} {sha9}"
```

Use `merge_template` when the repo can merge a whole batch in one call, and
`merge_per_pr_template` when each PR needs its own merge plus verify.

Any other key is optional and overrides the built-ins:

```yaml
    - name: lake-of-rage
      path: ~/Documents/lake-of-rage
      deploy_units:                 # replace the built-in table
        "api/": lor-api
        "transform/": dbt
      dbt_manifest_path: transform/target/manifest.json
      risk_globs: ["sql/*.sql"]     # extra paths that count as a migration
```

## Emitting for the dashboard

`--emit` writes the plan as a `merge.plan` event so `dash` can show
"ready to ship" batches. The `fb/fleetobs` `emit` command is feature-detected
and used when present; otherwise the plan is appended as a FleetEvent-shaped
line under the runs dir, so the dashboard still picks it up either way.
`--emit --json` reports which sink was used in `emitted_to`.

## Related lanes

This module is additive and owns no other lane's files. It reads:

* the `fb/fleetgate` status lines (`PREMERGE-APPROVED <sha>`);
* the `fb/fleetops` lane registry layout (`~/.agent-fleet/lanes/<operator>/`);
* the `fb/fleetobs` event stream, when it provides an `emit` command.

Every one of those is feature-detected or tolerant of absence: a missing lane
directory, a malformed lane file, an absent `emit` command, or a missing dbt
manifest all degrade to a smaller, clearly-reported plan rather than an error.
