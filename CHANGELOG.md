# Changelog

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
