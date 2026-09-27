# Post-merge hooks

Merging a PR that touches dbt models does not rebuild the lake. Something has to
read the diff, decide which tables moved, rebuild them, and tell the data side to
go and validate. That is currently a bash script per repo.

`fleet post-merge` makes it a configured per-repo feature. The repo supplies the
two domain-specific pieces; everything else — caching, label diffing, job
dedupe, note rendering — is shared and unit-tested with no lake and no network.

```
fleet post-merge --repo lake-of-rage --prs 412,415 --deploy-rc 0 --main-sha <sha>
```

The four steps, in order: **plan → label → trigger → hand off**.

## Config

Per repo, in `fleet.yaml`:

```yaml
post_merge:
  repos:
    - name: lake-of-rage
      path: ~/code/lake-of-rage
      plan_command: scripts/post_merge_plan.py
      trigger_command: scripts/rebuild.sh --job {job} --slot {slot}
      handoff_inbox: ~/.agent-fleet/handoff
      plan_timeout_seconds: 300
      trigger_timeout_seconds: 3600
      state_dir: ~/.agent-fleet/post-merge
```

| key | meaning |
| --- | --- |
| `plan_command` | Reads changed file paths on **stdin**, prints a plan as JSON on stdout. Required — without it the repo is reported unconfigured rather than guessed at. |
| `trigger_command` | Run once per job. Supports `{job}` and `{slot}`. Optional; without it jobs are reported `skipped`, never invented. |
| `handoff_inbox` | Directory the note and `INDEX` are written to. Omit to skip the hand-off. |
| `state_dir` | Plan cache + triggered-job ledger. Defaults to `~/.agent-fleet/post-merge`. |
| `*_timeout_seconds` | Ceilings on each subprocess. |

An **unknown key is an error**, not an ignored typo — a mistyped `plan_command`
would otherwise surface as a batch that silently never labels or rebuilds.

Commands are split with `shlex` and run **without a shell**, so a template can
quote its arguments but can never be re-interpreted.

## The plan contract

`plan_command` gets one changed file path per line on stdin and prints:

```json
{
  "models": ["gold_sales"],
  "jobs": [{"job": "dbt_sales", "slot": "s1"}],
  "heavy": true,
  "verify": ["dim_venue"]
}
```

* `models` — tables to rebuild.
* `jobs` — units of rebuild work. `job` is the dedupe key; `slot` is its
  concurrency lane.
* `heavy` — expensive enough to need the heavy tier.
* `verify` — tables to check but not rebuild.

Anything the planner prints that is not one of these keys is ignored. A
malformed *known* key is fatal: silently dropping a model list would label a PR
`rebuild:none` and skip its rebuild entirely.

**Results are cached per PR head sha.** A merged batch is not one PR — three PRs
may all touch `gold_sales` — and the planner is a dbt/Dagster query far too slow
to run per PR. A re-run of the same batch is free; a PR whose head moved plans
again.

## Labels

Each PR is converged onto exactly its plan's labels:

* `table:<model>` for each rebuilt model
* `verify:<model>` for each verify-only table
* `rebuild:heavy` | `rebuild:light` | `rebuild:none`

`rebuild:none` is a real answer, not an absent label: it means the planner looked
and found nothing to rebuild.

Stale labels from older plans are removed — a PR re-planned after new commits
must not keep claiming `table:gold_venues` it no longer touches. Removal is
restricted to the `table:`, `verify:` and `rebuild:` prefixes, so a human's
`bug` or the gate's status label is never stripped. A PR already carrying the
right labels makes no `gh` call at all.

## Triggering

Jobs are deduped **twice**:

1. **within a batch** — three PRs touching `gold_sales` queue one job;
2. **across batches** — a triggered job is recorded in a ledger and never re-run,
   so a retried or overlapping batch cannot double-fire a rebuild.

Jobs run **only when `--deploy-rc` is 0**. A failed deploy means main is not in a
rebuildable state, and queueing rebuilds against it produces work that has to be
thrown away. The note is still written, so the downstream agent knows the rebuild
is outstanding.

## The hand-off

One markdown note per merged batch, plus exactly one line appended to `INDEX`
(append-only, so a downstream agent can tail it and never re-reads):

```
# post-merge: lake-of-rage

- batch: 20260926T100000Z
- generated: 20260926T100000Z
- main: 4f2a91c
- deploy: ok
- rebuild tier: heavy

## PRs

### #412 widen venue filter

- head sha: `a1b2c3d`
- merge commit: `9e8d7c6`
- merged at: 2026-09-26T09:58:00Z
- rebuild: heavy
- tables:
- gold_sales
- verify-only tables:
- dim_venue

## Tables
- gold_sales

## Verify-only tables
- dim_venue

## Jobs queued
- dbt_sales (slot s1): ok
```

A table that is rebuilt is never also listed as verify-only. Empty sections say
`- (none)` rather than vanishing, so absence is legible.

This is the seam between "the code merged" and "the lake was rebuilt": a data
agent reads `INDEX`, opens the note, and runs or validates the queued jobs.

## CLI

```
fleet post-merge --repo REPO --prs N,N [--deploy-rc RC] [--repo-path PATH]
                 [--main-sha SHA] [--config PATH] [--no-handoff] [--json]
```

Exit `0` on success, `1` on a failed job or unreadable PR, `2` on a bad config
or an unconfigured repo.

## Wiring it into a merge

`post-merge` is deliberately a separate command from `fleet merge run`:
planning and labelling are safe to re-run, while triggering rebuilds touches
production. Call it after a batch deploys:

```bash
fleet merge run --repo-path ~/code/lake-of-rage
fleet post-merge --repo lake-of-rage --prs 412,415 --deploy-rc "$?" --main-sha "$(git rev-parse HEAD)"
```

Scheduling that call (a timer, a CI job) is an operator step, not something this
package installs.

## Tests

`tests/test_post_merge.py` covers plan caching, label diffing, job dedupe, note
rendering, and the whole flow — planner, forge, trigger, and inbox are all
faked, so nothing touches the network, `gh`, or a lake.
