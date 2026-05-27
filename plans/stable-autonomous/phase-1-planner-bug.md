# Phase 1 — Surface real planner errors

## Symptom

`dispatch-1517-retry2.log` and `dispatch-1690.log` both end with:

```
File "/home/evan/Documents/agent-fleet/agent_fleet/planner.py", line 206, in plan
    raise ValueError(last_error)
ValueError: No JSON object found in LLM output
```

The message is identical regardless of underlying cause: an empty `stdout`, a non-zero `exit_code`, a stderr stack trace from the cursor SDK, an auth failure, or a missing config. The session bisected for half an hour before guessing — correctly — that manual dispatch was loading a different fleet config because `AGENT_FLEET_TARGET_CONFIG` was unset. That guess could have been a glance at a log line.

## Root cause

Two cooperating layers swallow the diagnostic.

1. `agent_fleet/cursor_backend.py:312-320`. `CursorSession.send`'s bare `except Exception` returns a `CursorLLMResult(stdout="", stderr=str(exc), exit_code=1, ...)`. The exception type and traceback never reach the caller. `logger.exception` writes them to the agent_fleet logger, but the runner's log routing doesn't surface that to the per-dispatch log file.

2. `agent_fleet/planner.py:81-105` and `:169-206`. `_extract_json` raises `ValueError("No JSON object found in LLM output")` for any input where the JSON-decode walk fails. Empty stdout falls into this branch indistinguishably from "stdout has prose but no JSON." The retry loop at `:175-205` (`max_retries=2`, 3 attempts total) reads `result.stdout` at `:197` and never inspects `result.exit_code` or `result.stderr`.

There is also an upstream contributor: `agent_fleet/issue_loop/dispatch.py:37-189` does not require `AGENT_FLEET_TARGET_CONFIG`. When the env var is unset, `resolve_repo_config(workspace)` walks the filesystem looking for a `.agent-fleet/config.yaml` and may return a different target than the watcher used. The dispatch then runs against the wrong fleet config, which can cause the cursor session to fail in ways that look like "no JSON."

The planner-bug PR addresses (1) and (2). The config-resolution divergence is fixed in the same PR because it is the proximate cause of the failures the diagnostics will now reveal, and fixing the diagnostics without fixing the divergence would just produce louder versions of the same bug.

## Design

### Change 1: `CursorSession.send` keeps the same return shape but attaches the original exception

`CursorLLMResult` adds an optional `cause: BaseException | None = None` field. On the swallow path, the catch sets `cause=exc` before returning. Callers that want it can inspect `result.cause`; existing callers are unchanged.

This preserves the current contract for the eight callers that read only `stdout`. The follow-up PR (out of scope here) migrates them to raise on `exit_code != 0`. That migration is `migrate-callers-then-delete-legacy-apis` territory and earns its own PR.

### Change 2: `planner._extract_json` and the retry loop check `exit_code` and `stderr`

In `planner.py:175-205`, after `result = session.send(...)`:

- If `result.exit_code != 0` or `result.stdout.strip() == ""`, build an error message that includes `exit_code`, the last 500 chars of `stderr`, and (if present) `type(result.cause).__name__` and `str(result.cause)`. Do not retry on these — the cursor call itself failed, retrying will likely produce the same opaque failure. Raise immediately with the rich message.
- Only the "got prose but no JSON" case is worth retrying. Keep `max_retries=2` for that branch.
- The raised `ValueError` message format: `"PLAN cursor call failed: exit_code={n}, stderr_tail={...}, cause={...}"`. The runner's exception handler at `runner.py:736-748` already calls `logger.exception` which writes the full traceback; the message text is what shows up in `result.error` and on PR comments via the runner.

### Change 3: `dispatch.py` fails loudly when config resolution is ambiguous

`agent_fleet/issue_loop/dispatch.py:53-55` already exits 1 when `resolve_repo_config` returns None. Extend the check: if `AGENT_FLEET_TARGET_CONFIG` is unset *and* `find_repo_config` had to walk to find a match, log a warning naming the resolved path and the workspace. If both env vars (`AGENT_FLEET_WORKSPACE` and `AGENT_FLEET_TARGET_CONFIG`) are unset, exit 2 with a message telling the operator which env vars to set. This is not a code style change — it is the difference between a five-second log read and a thirty-minute bisect.

### Files touched

| File | Change |
|---|---|
| `agent_fleet/cursor_backend.py` | Add `cause` field to `CursorLLMResult`; populate in the catch at `:312-320`. |
| `agent_fleet/planner.py` | At `:175-205`, branch on `exit_code` and empty stdout; raise rich error. |
| `agent_fleet/issue_loop/dispatch.py` | At `:53-55`, distinguish unset-env vs walked-and-found-other; exit 2 on the former. |
| `tests/test_planner.py` (or equivalent) | Add test that `plan(...)` raises with `exit_code` in the message when `session.send` returns `CursorLLMResult(exit_code=1, stderr="boom")`. |
| `tests/test_cursor_session.py` | Add test that `CursorSession.send`'s swallow path populates `cause`. |
| `tests/test_dispatch.py` (or new) | Add test that `main()` exits 2 when both env vars are unset. |

## Verification

### Static

```
cd /home/evan/Documents/agent-fleet
uv run ruff check .
uv run pytest -q tests/test_planner.py tests/test_cursor_session.py
uv run pytest -q
```

### Runtime reproduction

Reproduce the original failure with the new diagnostics. From `/home/evan/Documents/agent-fleet`, with `AGENT_FLEET_TARGET_CONFIG` deliberately unset:

```
unset AGENT_FLEET_TARGET_CONFIG
ISSUE_NUMBER=1517 COMMENT_BODY="/agent --persona data" PERSONA=data \
  AGENT_FLEET_WORKSPACE=/home/evan/Documents/silphcoanalytics \
  python -m agent_fleet.issue_loop.dispatch 2>&1 | tail -40
```

Expected: exit 2 with a message naming `AGENT_FLEET_TARGET_CONFIG` as required. Not the old `ValueError: No JSON object found in LLM output`.

Then with the env var set but a deliberately bad cursor API key:

```
export AGENT_FLEET_TARGET_CONFIG=/home/evan/Documents/silphcoanalytics/.agent-fleet/config.yaml
CURSOR_API_KEY=invalid-on-purpose ISSUE_NUMBER=1517 ... python -m agent_fleet.issue_loop.dispatch 2>&1 | tail -40
```

Expected: `ValueError: PLAN cursor call failed: exit_code=1, stderr_tail=..., cause=...` with the cursor SDK's actual auth error visible in `stderr_tail` or `cause`.

### Live regression

The watcher path must keep working. Confirm by:

```
tail -n 0 -F ~/.agent-fleet/logs/watch.log &
gh issue comment 1736 --repo glassmarkets/silphcoanalytics --body '/agent --persona data'
# wait one watcher poll (~30 s)
```

Expected: the watcher logs `Issue #1736: dispatching persona=data` and spawns a process. The phase-1 changes must not alter the watcher's success path.

## Rollback

Single revert of the PR commit. No DB or state migration. `cause` field is additive on a dataclass; removing it is also additive in reverse.
