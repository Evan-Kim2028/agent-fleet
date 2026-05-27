# Two-pass PR analyzer — agent-fleet domain invariants

agent-fleet is the orchestration runtime. It spawns Cursor agents on issues,
runs a PR review/fix/merge loop, and shares one state file. Concurrency and
idempotency invariants are load-bearing. Flag any diff that violates them.

## Sole-writer rules (concurrency-critical)

- `agent_fleet/issue_loop/watcher.py` is the **sole writer** of
  `.agent-fleet-state.json` `in_flight` keys. `backlog_dispatcher.py` and
  scheduled jobs must NOT mutate `in_flight`; they only post `/agent` comments
  and let the watcher comment-trigger path admit the dispatch.
- A new module writing to `in_flight` is a HIGH finding. Look for
  `state["in_flight"]` or `set_state(...)` calls outside `watcher.py` and
  `in_flight.py`.

## FleetCapacityGate is the single admission point

- `agent_fleet/capacity/gate.py:FleetCapacityGate.try_admit` enforces:
  `already_in_flight` (same issue + persona), `issue_at_capacity` (count of
  runs for the issue ≥ `per_issue_limit`, default 3), `fleet_at_capacity`,
  `insufficient_ram`, plus visual-audit variants.
- `in_flight[issue_number]` is a LIST of `{pid, persona, visual_audit}` dicts.
  Multi-persona-per-issue (up to `per_issue_limit`) is intentional. A cheap
  pre-filter elsewhere that skips on "issue is in in_flight" is a HIGH
  finding — it makes gate semantics unreachable and contradicts watcher
  behavior.

## RETRYABLE_ADMISSION_REASONS vs continue-able reasons

- `RETRYABLE_ADMISSION_REASONS = {fleet_at_capacity, visual_audit_at_capacity,
  insufficient_ram, visual_audit_ram_reserved}` → `break` the dispatch loop
  (system-wide resource pressure; try again on the next tick).
- Any other refusal reason (`already_in_flight`, `issue_at_capacity`,
  `mutex_label`, `open_pr`, etc.) → `continue` to the next issue (issue-local
  problem; other issues may still admit).
- Flipping a `break` and `continue` for these is a HIGH finding — wrong
  retry semantics either starve the fleet or busy-loop.

## Background-task pid check rules

- `agent_fleet/in_flight.py:pid_is_dispatch(pid)` verifies a PID is a real
  dispatch process by inspecting `/proc/<pid>/cmdline` for
  `agent_fleet.issue_loop.dispatch` or `agent_fleet.schedule.task_dispatch`.
- `reap_in_flight(state)` filters entries to those whose pid passes
  `pid_is_dispatch`. Bypassing this filter (e.g. trusting `os.kill(pid, 0)`)
  is a HIGH finding — it leaves reaped/recycled-PID ghosts in `in_flight`.

## PR loop drift idempotency

- `agent_fleet/pr_loop/lifecycle.py` close+reopen+replan must require BOTH
  `pr_already_closed` AND `issue_already_replanned` to short-circuit.
  Skipping on `pr_already_closed` alone leaves the source issue stuck open
  with no replan marker.
- `post_pr_comment(_DRIFT_PR_MARKER)` must only fire AFTER a successful
  `reopen_issue`. Posting the marker before reopen succeeds creates a
  permanently stuck PR (marker present, issue still closed).

## /agent comment trigger semantics

- `/agent --persona <name>` on an issue is the ONLY dispatch trigger.
  `agent-running/<N>` labels are mutex locks applied by the dispatcher; they
  are NOT triggers.
- `backlog_dispatcher` posts `/agent` comments and relies on
  `issue_dispatch.enabled` on the same target. If `issue_dispatch.enabled`
  is false but `backlog_dispatcher.enabled` is true, comments are dead-lettered
  to GitHub with no consumer — a HIGH finding.

## Configuration and protected paths

- `agent_fleet/cursor_backend.py`, `agent_fleet/runner.py`,
  `agent_fleet/dispatcher.py`, `agent_fleet/phases.py`, `agent_fleet/hooks.py`,
  `agent_fleet/cli.py`, `.github/workflows/`, `pyproject.toml`, `uv.lock` are
  critical-path; structural changes need explicit justification.
- Do not silently catch exceptions in backend `send()` paths and return
  empty stdout — the planner depends on stderr/exit_code surfacing.

## Tests / verification

- Repo test command: `uv run --no-sync pytest -q`.
- Lint: `uv run --no-sync ruff check .`. Typecheck: `uv run --no-sync pyright`.
- New concurrency code MUST include a test that exercises both the admit and
  refuse paths through `FleetCapacityGate.try_admit` (see
  `tests/test_backlog_dispatcher.py` for the shape).
