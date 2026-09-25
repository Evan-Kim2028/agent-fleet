# Changelog

## Unreleased

### Added

- **`gate` pipeline — evidence-based PR merge gate:** `agent-fleet gate
  --repo-path <path> --pr <n> [--task-file f] [--status-file f]` runs an
  evidence-based gate against an existing PR head. A claim is not a blocker
  until a test demonstrates it: step0 runs the PR's own changed tests at head
  (per-package, `pyproject.toml`-dir aware; a pytest exit >= 2 is an infra error,
  never a finding), N parallel lens reviewers propose blockers only, and one
  verifier per claim must write a failing test that **the pipeline re-runs
  itself** — a verifier claiming CONFIRMED whose test passes is discarded. At
  most one judge call (plus one recheck) runs on a separately configured
  backend for untestable claims and its own blocker pass, whose new claims go
  back through verify. Writes `APPROVED(sha)` or `NEEDS_ESCALATION(reasons)` to
  the JSONL run log and, with `--status-file`, the automerge line
  `HH:MM:SS PREMERGE-APPROVED <sha9>` / `HH:MM:SS NEEDS-ESCALATION <reason>`.
- **Convergence instead of a fix-round cap:** the gate continues fixing while
  the failing set **strictly shrinks and no new failures appear**, and stops on
  evidence — 0 failing approves, no measurable progress escalates. Per-round
  metrics (`failing`, `fixed`, `new_failures`) are recorded so a stall is
  distinguishable from steady progress. `max_fix_rounds` (default 4) is only a
  safety net, reported as a distinct `cap` outcome.
- **`agent-fleet gate metrics`:** per-gate metrics appended to
  `~/.agent-fleet/gate/metrics.jsonl` (candidates, confirmed, rejected,
  untestable, per-round failing counts, terminal outcome), with
  `--format table` and `--limit N`.
- **Model policy:** a `fleet.yaml` `model_policy` pins the allowed models per
  backend and, via `roles`, which pipeline roles a backend may serve. The gate
  validates every dispatch **before** starting, so a config drift fails in a
  second rather than after a fan-out. Ships `examples/fleet.gate.yaml` with the
  approved policy: backend `cmd` → `stealth/space-bunny-alpha` only; backend
  `grok` → `step-5-preview` only, restricted to the `judge` role.
- **Machine-wide admission (`agent_fleet.slots`):** cross-process concurrency
  slots under `~/.agent-fleet/slots`, one lock file per slot held with an
  advisory `flock`. The kernel releases a slot when the holding process exits —
  including on `SIGKILL` — so a crashed run cannot leak capacity. Every backend
  session and every gate-held test run takes a slot, so several independent
  fleet processes share one budget. A separate, much smaller `test` pool bounds
  concurrent pytest processes, each of which is memory-capped via
  `systemd-run --user --scope -p MemoryMax=<test_memory> -p MemorySwapMax=0`
  (a runaway suite once consumed 36GB).
- **Gate contracts:** `agent_fleet/contracts/gate.py` with frozen dataclasses,
  hand-written draft-07 schemas in `agent_fleet/schemas/gate_{findings,verify,
  judge,recheck}.schema.json`, and `validate_*` functions, so a malformed model
  answer is rejected at the boundary rather than propagating into the blocker
  list.

### Fixed

- **Devin / concurrent `fleet run` worktree steal:** every single-task
  `fleet run` uses `task_index=0`, so resume attached to any dirty
  `fleet/task-0-*` branch — including one a live Devin dispatcher still
  owned. Devin sessions are cwd-keyed; the second run joined the first
  worktree and overwrote its session. Resume now skips worktrees whose
  sidecar lock is held by a different live PID (same-PID redispatch and
  stale locks after SIGTERM still resume).
- **Devin rate-limit classifier:** Cognition's live error is
  `Reached overall message rate limit` (plus
  `"cognition.ai/errorKind": "unavailable"`), not `Rate limited:`. Those
  used to classify as a hard `error` and skip retries. They now retry as
  `rate_limit` / `transient`.

## 0.15.2 — 2026-09-12

### Fixed

- **`changed_lines` / `collect_changed_files`:** measured working-tree delta
  vs `HEAD` first, so one stray dirty file (e.g. 2-line `uv.lock` churn)
  made a 300-line committed change report as 2 lines and the `code_review`
  gate silently skipped review. Diff is now counted against a resolved
  base (`origin/<default>` merge-base, else local `main`/`master`, else
  `HEAD^`) plus uncommitted and untracked lines. Fixes #90.
- **Hung verify commands:** `CommandVerifier` had no timeout, so one hung
  pytest could hold an admission slot forever. `verify_timeout_s`
  (default 600s) now bounds bootstrap (FATAL) and verify (RETRY). Fixes #89.
- **OpenRouter live Agnes test:** skip on transport timeout / connection
  error so a third-party blip does not fail CI or fleet verify.

### Changed

- **Grok default model:** `grok-4.6` (matches the grok CLI). Explicit
  `--model grok-4.5` is still honored.

## 0.15.1 — 2026-09-11

### Fixed

- **`tests/test_cli_persona_resolution.py`:** the test dispatched with
  `--backend devin`, so `require_backend_env()` short-circuited `cmd_run`
  before dispatch on any machine without real Devin credentials. It passed
  locally (credentials present) and failed in CI. The auth probe is now
  stubbed — the test is about persona resolution, not auth. Reproduce the
  old failure with `HOME=$(mktemp -d) uv run pytest
  tests/test_cli_persona_resolution.py`.

## 0.15.0 — 2026-09-11

### Summary

Four execution backends land in one release: **qwen**, **agnes**, **cmd**
(Command Code), and **devin** (Devin CLI). `0.14.1` was version-bumped but
never tagged; its qwen work ships here.

### Fixed

- **code_review auto_fix:** REQUEST_CHANGES/BLOCK from an advisory review now
  enter the fix loop. `review_blocking` still only controls whether the run
  itself goes red.
- **PR analyzer in code_review:** forwards the dispatch `fleet_config` so
  review uses the CLI backend/model (was leaking `~/.agent-fleet/fleet.yaml`).
- **PR analyzer diff:** includes unstaged and untracked execute output, not
  only `merge-base..HEAD`.

### Added

- **Devin CLI backend:** `register("devin", ...)` — headless `devin -p` on a
  Devin Pro/Team subscription (`devin auth login`; no API key). Default model
  `swe-2-high`, binary resolved from `PATH` or `~/.local/bin/devin` (override
  with `devin_bin`). Session id captured from `--export <tmp>.json` and reused
  via `-r` on the next send; retries on rate_limit/quota/transient/timeout
  resume the captured session rather than restarting. A rate-limit cooldown is
  now shared process-wide, so concurrent dispatcher sessions back off together
  instead of hammering the same quota. Session ids persist mid-flight, so a
  SIGTERM'd fleet still leaves a resumable session behind. `mode: plan` leaves
  `DEVIN_PERMISSION_MODE` unset (read-only). See `docs/DEVIN.md`,
  `examples/fleet.devin.yaml`.
- **`fleet run --complexity {LOW,MED,HIGH}`:** stops auto-classify from
  discarding `--pipeline`. MED/HIGH derive `code_review` (execute → review,
  plus repo verify when configured).
- **Command Code backend:** `register("cmd", ...)` — headless `cmd -p`
  (default `meituan/longcat-2.0:free`). Auth via `cmd login` /
  `~/.commandcode/auth.json`. Taste apply-only (`cmd_taste` or the Documents
  taste file; `--config taste-learning=false`). Session resume via
  `--resume`. Exit 8 (turn cap) is a partial success. See `docs/CMD.md`,
  `examples/fleet.cmd.yaml`.
- **Agnes backend:** `register("agnes", ...)` with `AGNES_API_KEY`, default model
  `agnes-2.5-flash`, optional `agnes_base_url` (default
  `https://apihub.agnes-ai.com/v1`). Thin registration over
  `openrouter_backend` — same pattern as Qwen. Prefer `max_parallel: 1` on free
  tier (~20 RPM). See `docs/AGNES.md`, `examples/fleet.agnes.yaml`.

## 0.14.1 — 2026-07-22

### Summary

Qwen is now a supported execution backend, reusing the existing OpenRouter
OpenAI-compatible HTTP client rather than shipping a separate module.

Pointing a new backend at real multi-file work on a live repo surfaced nine
pre-existing defects in the shared OpenRouter execution path — none of them
Qwen-specific; all of them affect the `openrouter` backend identically. Those
fixes are the bulk of this release. The most consequential: `backend.run()`
sent the model an empty tool array, so any pipeline phase taking the non-session
fallback path could not edit files at all.

### Added

- **Qwen backend:** `register("qwen", ...)` with `QWEN_API_KEY`, default model
  `qwen3.8-max-preview`, optional `qwen_base_url` (Alibaba Bailian Token Plan,
  OpenAI-compatible). Thin registration over `openrouter_backend` — no separate
  module. The same key also serves ~14 other Bailian models (`deepseek-v4-pro`,
  `kimi-k2.7-code`, `qwen3.7-max`, …) via `default_model`.
- **`edit_file` tool:** replaces one exact occurrence of `old_string` (errors on
  zero matches, and on multiple matches reports the count rather than silently
  replacing the first). Previously every modification round-tripped the entire
  file through the model twice via `read_file` + `write_file`.
- **Per-iteration progress logging:** one line per tool iteration
  (`iter 12/200: edit_file(path) | tokens=… elapsed=…s`) plus an exit summary
  with iteration count, tokens, elapsed, file-mutation count and exit reason.
  Runs previously produced no output at all until they finished, which made a
  45-minute run impossible to diagnose while in flight.
- **Thrash detection:** warnings at 25 and 50 consecutive iterations with no
  file mutation. Optional hard abort via `AGENT_FLEET_STALL_ABORT`, **disabled
  by default** so read-only/audit/plan personas are unaffected.
- **`.env` auto-loading** for the `fleet` CLI (dependency-free; real environment
  always wins; silent no-op when absent).

### Fixed

- **`OpenRouterBackend.run()` sent no tools.** It called the module-level
  `call_openrouter()`, which has no `tools` parameter, so the model received an
  empty tool array while `OpenRouterSession.send()` passed `_FILE_TOOLS`. Any
  phase on the `run()` fallback path silently produced zero file changes.
  `run()` now delegates to the session machinery.
- **Empty changesets were auto-approved.** Two vacuous-truth sites:
  `is_trivial_pr([])` returned `True` (an empty list trivially satisfies "all
  files are trivial"), and `decide_disposition` never checked `changed_files` on
  the `verify_ok` branch. An empty diff now routes to `completed_noop` and does
  not earn an `approve`.
- **The reviewer was never told the task goal.** `runner.py`'s `ReviewHandler`
  called `reviewer.review()` without `task_goal`/`task_context`/
  `implementation_summary`, so it judged diffs in isolation and could not detect
  off-task or incomplete work by construction. (`phases.py` was already correct;
  only the `code_review` pipeline path was affected.)
- **Tests-only changesets were auto-approved.** A diff containing only test
  files no longer earns a bare `approve` for a behaviour-change task; overridable
  via `allow_tests_only_approval` for genuinely test-only work.
- **`run_command` timed out at 60s** with no partial output — shorter than many
  real test suites, so "run the tests and make them pass" was unachievable and
  the model burned iterations retrying blind. Now defaults to 600s
  (`AGENT_FLEET_COMMAND_TIMEOUT_S`) and returns partial stdout/stderr on timeout.
- **Backend override inherited a mismatched `default_model`.** `--backend qwen`
  against a `fleet.yaml` configured for `grok` kept `grok-4.5` as the model. The
  yaml model is now inherited only when the resolved backend matches.
- **`fleet run` never configured logging.** `configure_fleet_logging()` was
  called only from `cmd_loop`, so `logger.info` output was dropped for the main
  run path.
- **Tool-iteration cap raised 80 → 200** (`OPENROUTER_MAX_TOOL_ITERATIONS`). 80
  was not enough for wide mechanical sweeps across ~15 files.

### Notes / known gaps

- `OPENROUTER_REASONING_EFFORT` is accepted but **ignored** by the Bailian
  endpoint — `low` and omitted produce identical output. It is a no-op for the
  qwen backend, not merely a badly-named knob.
- Qwen is verified on focused, well-specified tasks (a landing-header auth fix
  landed correctly with passing tests). It is **not** yet validated on
  discovery-heavy work: on tasks with an unknown root cause it explores without
  converging, and token burn on wide tasks reached 8–12M per run.
- `max_tokens=0` now floors to the session default (16384) rather than omitting
  the field — reasoning models return empty content under small provider
  defaults.

### Docs / examples

- `docs/QWEN.md`, `examples/fleet.qwen.yaml`, plus Qwen rows in `README.md`,
  `docs/FLEET-CONFIG.md`, `docs/PERSONAS.md` and `fleet.example.yaml`.

## 0.14.0 — 2026-07-15

### Summary

Grok token-usage accounting and a review-parse resilience retry. Also
un-breaks CI: the typecheck (`ty`) and version-consistency test jobs were
red on `main` since v0.13.0.

### Changes

- **grok_backend:** account per-session token usage by reading the Grok CLI's
  cumulative `updates.jsonl`, diffing consecutive reads into per-call deltas so
  rollups don't double-count; graceful no-op on missing/corrupt session data.
- **reviewer:** on a backend returning unparseable (prose/empty) output, do one
  strict JSON-only retry before failing the review phase.
- **ci fix — typecheck:** replace ineffective `# type: ignore` comments in
  `autonomy/parse_review.py` with `typing.cast(Severity, …)`; correct test
  annotations (`ModuleType | None`, `list[dict[str, object]]`) so `ty` passes.
- **ci fix — tests:** update the version-consistency test to the current
  release version.

## 0.13.2 — 2026-07-08

### Summary

Release hygiene: strip accidental runtime logs from the tree, tighten
`.gitignore`, and fix test isolation / doctor JSON assertions so the suite
is green for tagging.

### Changes

- **repo hygiene:** remove tracked dispatch/integration logs and root
  `efficiency-trail.tsv`; ignore `.tmp*`, `*.log`, and local experiment trails.
- **tests:** restore `agent_fleet.<backend>` package attrs after import-isolation
  tests (fixes monkeypatch targeting the wrong grok_backend module); doctor
  `--json` asserts payload shape `{backend, model, checks}`; ruff clean on
  recent grok/tool_env/pr_loop tests.
- **grok_backend:** use `Path.unlink(missing_ok=True)` for prompt-file cleanup.

## 0.13.1 — 2026-07-08

### Summary

code_review verify path runs `worktree_bootstrap_commands` before lint/test
(parity with CommandVerifier), so missing `node_modules` / `react-router` no
longer fails as a fixable verify error. Post-merge race: lifecycle/watcher skip
park/fix when the PR is already CLOSED/MERGED.

### Changes

- **phases.run_verify_phases:** run `worktree_bootstrap_commands` before persona
  verify commands; bootstrap failure fails the gate and stops further verify.
- **classify_verify_failure:** treat `command not found` / `: not found` as
  bootstrap (missing worktree tooling).
- **pr_loop:** pure `is_terminal_pr_state`; `park_for_human` and lifecycle body
  no-op when PR is already closed/merged; watcher records merged instead of park
  on decide PARK after terminal state.
- **tests:** bootstrap ordering for `run_verify_phases`; terminal-state guards.

## 0.13.0 — 2026-07-08

### Summary

Autonomy control plane for the PR loop: pure `decide(evidence) → Decision`
policy with SHA-keyed review address, early critical-path PARK, and merge
admission that never allows residual MEDIUM risk unless addressed for the
current head.

### Changes

- **agent_fleet/autonomy/**: new module — `types`, `parse_review`, `decide`
  (Phases 0–4). Invariants I1–I4 covered by unit tests.
- **ADR 0002:** documents evaluation order and state (`review_addressed_for_sha`).
- **pr_loop:** `use_autonomy_decide` (default true) wires lifecycle + watcher to
  `decide()` for needs_fix, early PARK, and merge admissibility; stores
  `review_addressed_for_sha` when review is addressed.
- **parse parity:** autonomy review parser matches `has_blocking_findings`.


## 0.12.3 — 2026-07-08

### Summary

Worktree commit path no longer dies with `No such file or directory: 'pre-commit'`.
Fleet ensures the `pre-commit` binary (PATH + best-effort `uv tool install`),
bootstrap installs hooks, and pr_loop re-runs worktree bootstrap before review/CI
fix commits so auto-merge can complete when CI is green.

### Changes

- **tool_env:** new helper — PATH augmentation (`~/.local/bin`), `which_tool`,
  `ensure_pre_commit` (auto-install via uv/pipx/pip --user).
- **github_ops:** commit preflight resolves pre-commit absolute path; clear error
  if missing; `_git_run` uses augmented PATH and distinguishes command-not-found
  from vanished worktree.
- **local_git:** git subprocesses inherit augmented PATH so hook scripts find
  pre-commit.
- **worktree-bootstrap.sh:** install pre-commit when config present; run
  `pre-commit install --install-hooks`.
- **pr_loop lifecycle:** re-run `worktree_bootstrap_commands` before commit/push
  on review-fix and CI-fix paths.
- **tests:** tool_env unit tests + preflight missing-binary coverage.


## 0.12.2 — 2026-07-08

### Summary

One-place backend selection for every entry point. `AGENT_FLEET_BACKEND` /
`AGENT_FLEET_MODEL` apply inside `load_fleet_config()` so CLI, pr-analyzer,
issue dispatch, and pr_loop all share the same override. CLI gains
`--backend` / `--model` on `fleet run` and `fleet doctor`, plus
`fleet config set-backend`.

### Changes

- **load_fleet_config:** resolves backend/model as kwargs → env → yaml → defaults.
- **CLI:** `fleet run --backend grok`, `fleet doctor --backend grok` (prints
  active backend/model), `fleet config set-backend grok`.
- **PR analyzer:** `resolve_fleet_config()` is a thin wrapper; env already
  applied by the loader.
- **Docs:** FLEET-CONFIG.md + GROK.md one-line backend switch section.

## 0.12.1 — 2026-07-08

### Summary

PR analyzer follows the fleet `default_backend` (same as `fleet run`). No more
hard-coded Cursor: Grok, Kimi, and OpenRouter runners use their own auth and
comment labels. The pr_loop watcher accepts analyses from any backend title.

### Changes

- **PR analyzer backend:** `github_action.py` loads `load_fleet_config()`, applies
  optional `AGENT_FLEET_BACKEND` / `AGENT_FLEET_MODEL` overrides only when set,
  authenticates via `require_backend_env` (env keys **and** Grok `auth_probe`),
  and builds the backend with `make_backend` — matching fleet run.
- **Comment titles / footers:** backend-derived labels (Composer / Grok Build /
  Kimi Code / OpenRouter) when `pr_review.comment_title` is still the stock
  value; custom titles are preserved.
- **pr_loop markers:** `find_reviewer_comment` matches Composer/Grok/Kimi/
  OpenRouter/Agent Fleet titles plus the stable `**Risk Level:**` line;
  `ignored_ci_checks` includes `fleet pr analysis` and `grok pr analysis`.
- **Workflows:** example + docs no longer hardcode `AGENT_FLEET_BACKEND=cursor`.

## 0.12.0 — 2026-07-08

### Summary

Grok Build CLI is now a first-class execution backend. Fleet runs can use the
official `grok` binary with **subscription-only** auth (`grok login` →
`~/.grok/auth.json`). No `XAI_API_KEY` is required. Doctor and CLI preflight
support a registry `auth_probe` for backends that authenticate outside env vars.

### Changes

- **Grok Build backend:** new `agent_fleet/grok_backend.py` — headless `grok`
  subprocess with `--prompt-file`, `--output-format plain`, model `grok-4.5`,
  `--yolo` (agent) or `--permission-mode plan` (plan), and durable sessions
  (`-s` UUID on first send, `-r` on subsequent). Scope notes for `path:`
  allowed_tools match the Kimi adapter.
- **Subscription auth probe:** `check_grok_auth()` verifies the `grok` binary
  and a non-empty valid JSON object at `~/.grok/auth.json`. Registered as
  `auth_probe` on the backend (no `env_var`).
- **Registry:** `_BackendSpec.auth_probe`, `backend_auth_probe()`,
  `backend_is_registered()`; `default_backend: grok` with optional `grok_bin`.
- **Doctor / CLI:** `_check_backend_key` and `require_backend_env` prefer
  `auth_probe` when present, then env-var keys, then pass for registered
  backends with neither.
- **Docs / examples:** `docs/GROK.md`, `examples/fleet.grok.yaml`, README /
  PERSONAS / fleet.example.yaml rows for Grok.


## 0.11.4 — 2026-07-08

### Summary

OpenRouter backend hardening: the fleet now runs fully autonomous, end-to-end
tool-use sessions on OpenRouter models, with the guards, retries, and budget
controls needed to make that reliable in practice. Validated live — the free
`tencent/hy3:free` model completed a real multi-file bug fix (silphco #2312)
through the entire pipeline, and two such tasks ran concurrently without
issues.

### Changes

- **Repetition + hallucination guards:** `OpenRouterSession.send()` detects
  repetition loops (a 50-char substring repeated 5+ times) and hallucinated
  completion claims made before any tool has been called, and injects a
  corrective prompt. Up to 3 corrections are attempted; if the model still
  hasn't produced usable output, the run now fails loudly with `exit_code=1`
  instead of silently accepting bad output.
- **Text-mode tool-call fallback + usage normalization:** models that emit
  tool calls as plain text (instead of the structured tool-call API) are
  still parsed and dispatched; `llm.usage` reporting is normalized across
  response shapes.
- **Retry/backoff on transport errors:** 429s, 5xx responses, and transport
  failures are retried up to 3x with exponential backoff, honoring
  `Retry-After` when present.
- **Bounded conversation history:** once history exceeds 400K chars, older
  tool-result bodies are elided to keep long sessions under the context
  limit.
- **Scope-guarded `run_command`:** obviously destructive invocations (`rm -rf`
  outside scope, `git clean`, `git reset --hard`) are blocked when write
  scopes are configured.
- **Exception-safe tool execution:** `_execute_tool` now wraps handler
  exceptions and returns a JSON tool-error the model can recover from instead
  of killing the session. Fixed a `list_files` crash from sorting raw dicts
  (now sorts by `(type, name)`).
- **Reasoning-effort control + adaptive `max_tokens`:** `OPENROUTER_REASONING_EFFORT`
  (default `high`) is sent to reasoning models; on reasoning exhaustion,
  `max_tokens` escalates (doubling up to 65536) before failing. The escalated
  floor is now sticky per session, so subsequent iterations start there
  instead of re-exhausting the low base budget every turn — eliminating a
  doomed low-budget retry per iteration on long sessions. On a real IMPLEMENT
  task this took a run from ~1hr (previously killed) down to ~4 minutes.
- **Configurable tool-iteration cap:** `OPENROUTER_MAX_TOOL_ITERATIONS`
  (default 80) bounds the tool-use loop; history trimming keeps long
  sessions bounded even at higher caps.
- **Dynamic per-task skill loadouts:** `--skills`, `--add-skills`, and
  `--loadout {minimal,standard}` let the dispatching host assign a smaller
  skill set per task instead of always loading the full execute loadout;
  `default_loadout_size` in `fleet.yaml` sets the fleet-wide default.
- **VERIFY fails closed on indeterminate git state:** `get_changed_files`
  no longer fails open — it resolves the diff base through a fallback chain
  (origin → local `main`/`master` → fork point) so committed changes are
  detected even without an `origin` remote, and reports whether detection was
  determinate. When change detection genuinely cannot tell what changed,
  VERIFY now returns `RETRY` instead of silently reporting a false-clean
  "No changes detected" pass; a legitimately empty diff still passes.

## 0.11.3 — 2026-07-07

### Summary

Added OpenRouter as a third execution backend (HTTP via stdlib `urllib`,
default model `tencent/hy3:free`) and made the entire fleet backend-agnostic
so an openrouter-only or kimi-only install never imports `cursor_backend`.

### Changes

- **OpenRouter backend:** new `agent_fleet/openrouter_backend.py` — talks to
  OpenRouter's `/api/v1/chat/completions` endpoint using only `urllib.request`
  (no new runtime dependency). Default model `tencent/hy3:free`. Handles
  reasoning models (surfaces a clear error when `max_tokens` is too low for
  the model to produce content after reasoning).
- **Lazy backend imports:** the three backend modules are imported lazily
  inside their factory functions in `backends.py`. Selecting `openrouter`
  never imports `cursor_backend` or `kimi_backend` — the "all or nothing"
  import-graph guarantee. Keystone `test_import_isolation` gates this.
- **NoopSession decoupled:** `noop_session.py` owns `NoopLLMResult` (a
  protocol-compliant `LLMResult` dataclass) instead of importing
  `CursorLLMResult`. 8 stub test files migrated to `NoopLLMResult`.
- **Registry-driven doctor SDK check:** `doctor.py` reads
  `backend_sdk_import_check(backend)` from the registry. Cursor declares
  `sdk_import_check="cursor_sdk"`; kimi and openrouter declare `None`. An
  openrouter-only install never sees a `cursor_sdk` warning.
- **Config defaults are backend-agnostic:** `FleetConfig.default_model` and
  `Persona.model` default to `None`; each backend supplies its own
  `DEFAULT_MODEL` constant. The cursor slug band-aids in the kimi and
  openrouter factories are deleted — switching backends now requires
  switching `default_model` (or unsetting it to inherit the backend default).
- **DAG canvas and pr_loop defaults:** `dag/canvas_state.py` uses `"inherit"`
  instead of a cursor slug; `pr_loop/config.py` includes `openrouter pr
  analysis` in the default ignored CI checks.

## 0.11.2 — 2026-06-01

### Summary

Deepened the Run pipeline so the Fleet can govern and salvage its own runs
instead of spiralling and stranding worktrees. Four stacked seams turn the
open-loop static pipeline into a closed loop with explicit disposition,
control, and fix-strategy seams behind `run()`.

### Changes

- **C1 — Disposition seam:** new `agent_fleet/disposition.py` with a pure
  `decide_disposition(RunFacts, policy) -> Disposition`. The four terminal sites
  in `runner.py` build `RunFacts` and execute the returned `Disposition`.
  Failed-verify-with-changes and scope-violation now salvage to a labeled draft
  PR; a FATAL verifier tripwire always abandons.
- **C2 — Run Controller seam:** new `agent_fleet/run_controller.py` with
  `ThresholdController`. The fix/total token ratio is extracted into one
  `phase_token_counts` helper reused by both `build_cost_alerts` and the
  controller; `RunLog` gains a live-usage accessor. HALT and ABANDON route into
  the C1 salvage disposition, breaking the FIX spiral on its own signal.
- **C3 — Fix Attempt memory seam:** new `agent_fleet/fix_attempt.py` with
  `FixMemory` and a `FixStrategy` protocol. `ColdRestartStrategy` is the default
  and preserves current behavior; `WarmContinuationStrategy` is gated behind a
  `fix_strategy` config flag. The duplicated truncate helper is removed.
- **C4 — Phase executor:** `execute_graph` and a `PhaseHandler` protocol in
  `phase_graph.py`; `run()` delegates to the executor instead of hand-coding the
  phase sequence.

## 0.11.1 — 2026-05-30

### Summary

Unified CLI surface, internal seam cleanup, and docs hard-update.  All commands
now route through the single `fleet` entry point.

### Changes

- **P0 — Pre-flight fix:** corrected `except OSError, ValueError:` → `except (OSError, ValueError):` in `cli.py`; added `cmd_doctor` test for malformed `.agent-fleet.yaml` with backend fallback.
- **P1 — FleetContext:** new `agent_fleet/context.py` with `FleetContext`, `ContextOptions`, and `build_fleet_context`; migrated `cmd_review`, `cmd_scope`, `cmd_scout`, `cmd_run`, `cmd_personas`, `cmd_loop`, `cmd_learn`; `cmd_doctor` stays inline.
- **P2 — normalize_argv + summon:** new `agent_fleet/cli_core.py` with `normalize_argv`; `summon` subcommand for idempotent first-run setup; `allow_abbrev=False` on the top-level parser.
- **P3 — Entry point fold:** `fleet = agent_fleet.cli:main` added; `pr-analyze`, `watch`, `dispatch`, `schedule` subcommands wired into the unified parser; old console-script entries kept as undocumented shims.
- **P4 — emit:** new `agent_fleet/emit.py` with explicit `status → exit-code` table; migrated postambles in `cmd_review`, `cmd_scope`, `cmd_scout`, `cmd_run`, `cmd_personas`.
- **P5 — pr-loop shim:** deleted `agent_fleet/pr_loop/cli.py`; new `agent_fleet/pr_loop/_shim.py` prepends "loop" and delegates to the unified parser; `agent-fleet-pr-loop` repointed at the shim.
- **P6 — Docs + version:** hard-updated `README.md`, `docs/QUICKSTART.md`, `docs/NEW-REPO.md`, `docs/FLEET-CONFIG.md`, `docs/PERSONAS.md`, `docs/SCHEDULES.md`, `examples/repo.agent-fleet.yaml` to the `fleet` surface; added `docs/adr/0001-disable-argparse-abbreviation.md`; bumped version to `0.11.1`.
- **`fleet self update`:** new `self update` subcommand upgrades the globally installed tool via `uv tool upgrade agent-fleet`.
