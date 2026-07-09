# Changelog

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
