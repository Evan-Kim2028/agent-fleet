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
downstream closure so the rebuild covers everything the change can invalidate.
dbt's `parent_map` points *upstream* (each node lists what it reads), so the
dependents are its inverse — the same relation the manifest calls `child_map` —
and that is what gets walked. Without a manifest only the directly edited
models are selected, and the plan records `dbt_select_source: direct` so the
operator knows the difference.

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
previous result — the tree it writes is committed back into a commit first,
because git >= 2.38 rejects a bare tree as the next merge base. On older git
(this box runs 2.34) it falls back to a scratch worktree under a temp
directory, replaying the merges with `git merge --no-commit` and committing
each result, since git refuses a second merge while `MERGE_HEAD` is still
outstanding. The worktree is created and removed by the check itself; your
repo is never mutated. If a batch still cannot merge cleanly, it is peeled
back to single-PR batches rather than shipping an unlandable merge.

The head commits come from the GitHub API, so a checkout that has not fetched
since a PR opened does not have them. They are fetched (`refs/pull/<n>/head`,
falling back to the raw SHA) before the check runs — otherwise every missing
object would read as a conflict and silently collapse the plan back to one
deploy per PR. A commit that cannot be fetched at all is still treated as
unmergeable, so a batch is never reported as verified without being checked.

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

## Running the plan: `fleet merge run`

`merge-plan` decides; `merge run` ships. One tick plans, then takes every
eligible batch through **merge → deploy → verify**:

```
fleet merge run                       # one tick, then exit (cron / CI friendly)
fleet merge run --daemon 180          # loop, SIGINT/SIGTERM to stop
fleet merge run --dry-run             # report decisions, run nothing, take no lock
fleet merge holds                     # what is holding merges, and why
fleet merge release <hold>            # clear a named cluster hold
```

`--dry-run` is the safe way to try a config change: it runs the same planning
and reports the same outcomes without spawning a command or creating a lock
file.

Per tick, per batch, the outcome is one of `merged`, `held`, `locked`,
`needs_rebase`, `failed`, or `skipped` — and a stopped batch always carries the
reason, so a queue that is not moving says why. Exit code is `1` only when a
batch actually failed; held and locked batches are the scheduler working and
must not page a monitor.

Events go through the normal fleet event path (`RunLog`), so they land in the
runs-dir JSONL like everything else: `merge.start`, `merge.start_batch`,
`merge.merged`, `merge.deployed`, `merge.verified`, `merge.needs_rebase`,
`merge.failed`, `merge.held`, `merge.locked`, `merge.end`.

### Commands per repo

`merge run` holds no repository knowledge. Every step comes from config:

```yaml
merge_plan:
  repos:
    - name: lake-of-rage
      path: ~/Documents/lake-of-rage
      merge_template:  "scripts/lake_batch_merge.sh {pr_args}"
      deploy_template: "scripts/lake_deploy.sh {merge_sha}"
      verify_template: "scripts/lake_verify.sh {merge_sha}"
      rebase_template: "scripts/rebase_regate.sh {lane} {repo} {pr} {sha9}"
```

| Placeholder | Expands to |
|---|---|
| `{pr_args}` | `12:1a2b3c4 14:5e6f7a8` — every merged PR in the batch |
| `{pr}` / `{sha9}` | one PR number / its approved SHA, short form |
| `{merge_sha}` | the **merge commit** that the deploy builds, not the reviewed head |
| `{repo}` / `{lane}` | repository name / lane name |

`deploy_template` and `verify_template` are optional; a repo with neither stops
after the merge. A repo with no merge template at all is reported as
`(no merge template configured)` — the executor never invents a command.

Commands are `shlex`-split and executed **without a shell**, so a template can
quote its arguments but can never be re-interpreted. A command that overruns
`command_timeout_seconds` is killed, along with the children it backgrounded, by
the process group the executor started for it — never by any other pid. Output
collection is bounded separately, so a command that leaves a grandchild holding
the pipe cannot hold the executor open past the ceiling.

### Why the deploy lock is not a marker file

The ad-hoc scripts guarded deploys with `touch lake_deploying` and removed it on
exit — so a merge that stopped on a conflict leaked the marker, and the next
repository's merge waited on it for an hour and forty minutes.

`DeployLock` takes an `flock` on an open file descriptor. The lock belongs to
that descriptor, so the kernel releases it when the process ends — on a normal
return, on an exception, on `KeyboardInterrupt`, or on a crash. There is no exit
path that can leak it, and the release is a `finally` rather than a `trap` that a
later code path can forget.

A sidecar `<repo>.meta` records the holder's pid, boot id, and process start
time. It is advisory — it explains the lock, it never grants it. A live holder is
**never** displaced; a record whose pid is gone, whose pid the kernel has since
recycled, or which predates the last reboot is stale and gets reclaimed.

### Conflicts do not block the queue

A `CONFLICTING` PR is never retried in place. The tick marks it
`needs_rebase`, drops it from the batch, and hands it to `rebase_template` (or
the executor-wide `rebase_command`) once. The rest of the batch ships normally,
and so does every other repository.

The rebase pushes a new head, so the PR re-enters the gate naturally; the
stale-approval rule then correctly refuses to merge it until a fresh approval
lands for that head. A merge command that exits with `conflict_exit_code`
(default `3`, matching the existing merge scripts) is treated the same way.

### Nothing is remembered that GitHub already knows

There is no "already merged" list. Every tick re-reads live PR state and decides
from it, so a PR is skipped only for a checkable reason:

| Live state | Outcome |
|---|---|
| `state == MERGED` | skipped — already shipped |
| head does not start with the approved SHA | skipped — **stale approval** |
| `state == OPEN`, `mergeable == MERGEABLE` | eligible |
| `mergeable == CONFLICTING` | `needs_rebase`, handed to the rebase command |
| anything unreadable or `UNKNOWN` | skipped — never assumed safe |

A hand-maintained hold list was once seeded with in-flight lanes, and twelve
approved PRs sat unmerged because the list and reality disagreed. The ledger the
executor does keep holds only operator intent and fairness counters — never merge
bookkeeping.

### Cross-repo ordering

`exclusive_groups` names repos that must never deploy at the same time, and
`post_merge_hold_seconds` adds a quiet period after a deploy:

```yaml
merge_plan:
  executor:
    exclusive_groups: [["lake-of-rage", "silphcoanalytics"]]
    post_merge_hold_seconds: 300
```

Two different rules, deliberately kept apart:

* **Exclusion** — at most one repo per group deploys in a single tick, whatever
  order the plan produced. The loser is reported `held` and goes next tick. A
  repo is not excluded by its *own* earlier batch: it is sharing the deploy
  surface with a peer, not with itself, so a repo drains its own queue in one
  tick.
* **Turn taking** — the repo that deployed last yields to its peer, so a repo
  with constant work cannot starve one with a single batch ready. A turn is
  only ever owed to a peer that is *in the plan*: a peer with no batch has no
  turn to take, and holding the busy repo for one that never comes is a
  self-inflicted deadlock, not fairness. The group looks at the whole plan
  rather than at batch order, so a peer listed after the batch asking still
  counts as waiting.

A batch that breaks more than one rule says so in one line, e.g.:

```
[HELD] silphcoanalytics #2 (batch 1)  post-merge hold for lake-of-rage+silphcoanalytics until 300s; exclusive group lake-of-rage+silphcoanalytics: lake-of-rage went last, silphcoanalytics has the turn
```

A `--dry-run` reports those same outcomes without moving any of them: it runs
no command, takes no lock, and writes nothing to the ledger, so previewing a
config change cannot consume a group's turn or hold its peer.

### A merge that landed but never deployed

GitHub reports a merged PR as `MERGED` forever, so a batch whose merge landed
and whose deploy then failed is never eligible again — every later tick saw
"already merged", reported `skipped`, and exited 0 while production had never
seen the work. The executor therefore records, before running the deploy, that
this batch's deploy is still owed, and retries it on the next tick. The record
is the one fact GitHub does not keep; it is dropped as soon as the deploy and
verify commands both succeed, so nothing is retried forever.

### Cluster holds

A hold blocks merges matching a lane pattern or deploy unit until an operator
releases it by name:

```yaml
merge_plan:
  executor:
    holds:
      - name: pass2-downstream
        match:
          lanes: ["sales-pass2-*"]
          deploy_units: ["dbt"]
```

```
$ fleet merge holds
held: pass2-downstream  [lanes sales-pass2-*]
  release: fleet merge release pass2-downstream

$ fleet merge release pass2-downstream
released pass2-downstream
```

### Executor settings

Unlike the repo entries, which stay permissive, the `executor` block is
validated **strictly**: an unknown key raises rather than being silently dropped,
because a mistyped key disables a safety setting without saying so.

| Key | Default | Meaning |
|---|---|---|
| `holds` | `[]` | named cluster holds (above) |
| `exclusive_groups` | `[]` | repos that must not deploy concurrently |
| `post_merge_hold_seconds` | `0` | quiet period after a deploy |
| `rebase_command` | `""` | fallback when a repo sets no `rebase_template` |
| `command_timeout_seconds` | `1800` | ceiling on one command |
| `conflict_exit_code` | `3` | exit code meaning "conflicting" |
| `state_dir` | `~/.agent-fleet/merge` | locks and ledger live here |

Running the tick on a schedule is an operator step — this command does not
install a timer or service unit. Point cron, or a supervisor you already run, at
`fleet merge run`.

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
