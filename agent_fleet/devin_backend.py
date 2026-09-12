"""Devin CLI backend — headless ``devin -p`` (Cognition's Devin agent).

Auth is Devin's own OAuth login (``devin auth login``), stored in
``~/.local/share/devin/credentials.toml``. Fleet never injects a Devin API
key.

Verified against ``devin`` 3000.10.21 (``~/.local/bin/devin``):

- Non-interactive runs use ``devin --prompt-file <file> -p --model <id>
  --respect-workspace-trust false`` (``--prompt-file`` avoids argv length
  limits; a bare ``-p`` with no inline text reads the prompt file).
- Resuming an existing conversation is ``devin -r <SESSION_ID> ...`` — the
  session id persists across resumes (verified: same id after two ``-r``
  round trips).
- **Session id capture**: every invocation also passes ``--export
  <tmp>.json``. Devin writes (or overwrites) that file after each turn with
  a top-level ``"session_id"`` field (e.g. ``"nervous-uniform"``) — this is
  the id ``-r`` expects. Reading that field is O(1), race-free even with
  concurrent sessions in the same cwd, and does not require an extra
  subprocess. ``devin list --format json`` (which lists ``{id,
  working_directory, last_activity_at, ...}`` for the cwd) was verified to
  work too and is a documented fallback path, but is not used here — export
  parsing was simpler and strictly better for this use case.
- Non-interactive mode cannot show approval prompts. Auto-approving all
  tool calls requires the environment variable ``DEVIN_PERMISSION_MODE=bypass``
  (verified: without it, tool calls that would edit the workspace are
  rejected with exit code 2 and "rejected a tool call that requires
  confirmation"). Fleet sets this env var per-call for ``mode != "plan"``
  and leaves it unset (default ``auto`` — read-only tools only) for
  ``mode == "plan"``.
- Auth: OAuth via ``devin auth login`` writes
  ``~/.local/share/devin/credentials.toml`` (TOML) with a non-empty
  ``windsurf_api_key`` field. ``check_devin_auth`` probes the file directly
  (no subprocess) rather than shelling out to ``devin auth status``, mirroring
  ``grok_backend.check_grok_auth``.
- Error text prefixes observed/documented on stdout+stderr: "Rate limited:",
  "Quota exhausted:", "Server error:", "Request timed out:", "Usage limit
  reached", "Usage paused", "Connection failed (attempt". ``call_devin``
  retries rate_limit/quota/transient/timeout failures with exponential
  backoff + jitter, resuming the captured session id (rather than
  restarting) on each retry.
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.observability.context import get_run_context, get_run_log
from agent_fleet.session_store import persist_session_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.contracts.mcp import McpServerSpec
    from agent_fleet.contracts.mcp_requirement import McpRequirement

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "swe-2-high"

# OAuth credentials written by ``devin auth login``. Module attribute (not a
# local constant) so tests can monkeypatch it, mirroring grok_backend.AUTH_JSON.
CREDENTIALS_PATH = Path("~/.local/share/devin/credentials.toml").expanduser()

# Devin CLI's local session store (``message_nodes.chat_message`` JSON carries
# per-turn ``metadata.metrics`` usage). Read-only fallback when the
# ``--export`` file has no usage yet. Module attribute so tests can
# monkeypatch it, mirroring CREDENTIALS_PATH.
SESSIONS_DB_PATH = Path("~/.local/share/devin/cli/sessions.db").expanduser()

# Retry/backoff knobs. Overridable via DEVIN_MAX_RETRIES / DEVIN_RATE_LIMIT_COOLDOWN_S
# (read at call time in call_devin, not at import time, so tests can monkeypatch env
# per-test without reloading the module).
_MAX_RETRIES = 4
_BACKOFF_BASE_S = 5.0
_BACKOFF_CAP_S = 120.0
_BACKOFF_JITTER = 0.3
_RATE_LIMIT_COOLDOWN_S = 60.0

_RETRYABLE_CLASSIFICATIONS = frozenset({"rate_limit", "quota", "transient", "timeout"})

# Process-wide (module-level) rate-limit cooldown, shared across every
# concurrent devin session in this process (dispatcher runs tasks on a
# ThreadPoolExecutor in one process — see agent_fleet/dispatcher.py). Without
# this, each session's retry/backoff is independent: if one session gets
# rate-limited, the others keep hammering the same account/quota instead of
# backing off too. Guarded by _rate_limit_lock; monotonic clock so it is
# immune to wall-clock adjustments.
_rate_limit_lock = threading.Lock()
_rate_limit_until_monotonic: float = 0.0


def _await_shared_rate_limit(
    sleep: Callable[[float], None], *, already_satisfied_until: float = 0.0
) -> None:
    """Block until any cooldown set by another concurrent session has elapsed.

    ``already_satisfied_until`` lets a call skip waiting on a deadline *it*
    already extended (and already backed off for via its own retry sleep) —
    without it, a single session's own rate-limit backoff would double-sleep
    on its very next attempt, since checking the still-current shared deadline
    again would look unsatisfied.
    """
    with _rate_limit_lock:
        until = _rate_limit_until_monotonic
    if until <= already_satisfied_until:
        return
    remaining = until - time.monotonic()
    if remaining > 0:
        sleep(remaining)


def _extend_shared_rate_limit(cooldown_s: float) -> float:
    """Record that *this* session was rate-limited so siblings also back off.

    Returns the resulting deadline (monotonic time) so the caller can pass it
    back as ``already_satisfied_until`` and avoid double-waiting on its own
    contribution.
    """
    global _rate_limit_until_monotonic
    with _rate_limit_lock:
        _rate_limit_until_monotonic = max(
            _rate_limit_until_monotonic, time.monotonic() + cooldown_s
        )
        return _rate_limit_until_monotonic


# How often the background progress thread polls for live usage while a devin
# subprocess is running. Overridable via DEVIN_PROGRESS_POLL_S for tests.
_PROGRESS_POLL_INTERVAL_S = 10.0

# Keys accepted by RunLog.llm_usage() — mirrors grok_backend._RUN_LOG_USAGE_FIELDS.
_RUN_LOG_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# Cumulative usage last seen per devin session id. Devin's own counters
# (export ``final_metrics`` and the sessions.db fallback) are cumulative for
# the whole session, not per-call (verified live: a second turn on the same
# session roughly doubled the totals), so repeated turns against the same
# resumed session must be diffed against this to log a per-call delta.
_last_session_usage: dict[str, dict[str, int]] = {}

# (needle, classification) checked case-insensitively, in order, against
# stdout+stderr. See module docstring for where these prefixes come from.
_ERROR_CLASSIFIERS: tuple[tuple[str, str], ...] = (
    ("rate limited:", "rate_limit"),
    ("quota exhausted:", "quota"),
    ("usage limit reached", "quota"),
    ("usage paused", "quota"),
    ("server error:", "transient"),
    ("connection failed (attempt", "transient"),
    ("request timed out:", "timeout"),
)


def _find_devin_bin() -> str:
    found = shutil.which("devin")
    if found:
        return found
    local = Path("~/.local/bin/devin").expanduser()
    if local.exists():
        return str(local)
    return "devin"


def check_devin_auth() -> tuple[bool, str, str]:
    """Probe Devin CLI + OAuth login. Returns ``(ok, detail, fix)``.

    Does **not** shell out to ``devin auth status`` — reads
    ``CREDENTIALS_PATH`` directly (fast, no subprocess), mirroring
    ``grok_backend.check_grok_auth``.
    """
    bin_path = _find_devin_bin()
    if not Path(bin_path).exists() and shutil.which(bin_path) is None:
        return (
            False,
            "devin binary not found",
            "install the Devin CLI (https://devin.ai/) or set devin_bin in fleet.yaml",
        )

    if not CREDENTIALS_PATH.is_file():
        return False, f"{CREDENTIALS_PATH} missing", "run `devin auth login`"

    try:
        raw = CREDENTIALS_PATH.read_bytes()
        if not raw.strip():
            return False, f"{CREDENTIALS_PATH} is empty", "run `devin auth login`"
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return False, f"{CREDENTIALS_PATH} invalid: {exc}", "run `devin auth login`"

    key = data.get("windsurf_api_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip():
        return (
            False,
            f"{CREDENTIALS_PATH} missing windsurf_api_key",
            "run `devin auth login`",
        )
    return True, f"authenticated ({CREDENTIALS_PATH})", ""


def classify_devin_error(text: str) -> str | None:
    """Classify a devin failure from combined stdout+stderr text.

    Returns ``"rate_limit"``, ``"quota"``, ``"transient"``, ``"timeout"``, or
    ``None`` (not a recognized/retryable failure).
    """
    lowered = (text or "").lower()
    for needle, classification in _ERROR_CLASSIFIERS:
        if needle in lowered:
            return classification
    return None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _backoff_delay(attempt: int, *, min_wait: float | None = None) -> float:
    """Exponential backoff with jitter, capped, and optionally floored at *min_wait*."""
    base = min(_BACKOFF_BASE_S * (2**attempt), _BACKOFF_CAP_S)
    jittered = base * (1 + random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER))
    jittered = max(jittered, 0.0)
    if min_wait is not None:
        jittered = max(jittered, min_wait)
    return jittered


def _read_export_session_id(export_path: str) -> str | None:
    """Best-effort read of ``session_id`` from a devin ``--export`` JSON file.

    Never raises: a missing file, unreadable file, or malformed JSON is
    treated as "no session id available yet".
    """
    try:
        path = Path(export_path)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _coerce_int(value: object) -> int:
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _read_export_usage(export_path: str) -> dict[str, int] | None:
    """Best-effort read of cumulative token usage from a devin ``--export`` file.

    Devin writes ``final_metrics`` (``total_prompt_tokens``,
    ``total_completion_tokens``, ``total_cached_tokens``) once, at the end of
    the turn — verified live: the export file does not exist yet while a
    multi-step turn is still running, so this is a *cumulative* total for the
    session so far, not a per-call delta. Devin's export has no separate
    cache-write/creation counter, so ``cache_write_tokens`` is always 0 here.
    Never raises: a missing file, unreadable file, or malformed/incomplete
    JSON is treated as "no usage available yet".
    """
    try:
        path = Path(export_path)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    metrics = data.get("final_metrics")
    if not isinstance(metrics, dict):
        return None
    usage = {
        "input_tokens": _coerce_int(metrics.get("total_prompt_tokens")),
        "output_tokens": _coerce_int(metrics.get("total_completion_tokens")),
        "cache_read_tokens": _coerce_int(metrics.get("total_cached_tokens")),
        "cache_write_tokens": 0,
    }
    return usage if any(usage.values()) else None


def _resolve_live_session_id(*, work_dir: str, since_ts: float) -> str | None:
    """Find the most recent devin session row for *work_dir* created at/after *since_ts*.

    Used only when a session id has not been captured from the export file
    yet (e.g. progress polling mid-way through a fresh session). Best-effort
    against Devin's live sqlite db: never raises.
    """
    if not SESSIONS_DB_PATH.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{SESSIONS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT id FROM sessions WHERE working_directory = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (work_dir, int(since_ts) - 5),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _read_sessions_db_usage(session_id: str) -> dict[str, int] | None:
    """Fallback usage read from Devin's local sqlite db, read-only.

    ``message_nodes.chat_message`` is a JSON blob whose ``metadata.metrics``
    carries ``input_tokens``/``output_tokens``/``cache_read_tokens``/
    ``cache_creation_tokens`` — but only on assistant turns. **Verified
    live these are per-turn deltas, not a running cumulative total**: a
    multi-step run's summed metrics across all turns (~2.5M tokens) were
    ~37x the single latest turn's metrics (~67k) — the earlier assumption
    here (single latest row == cumulative) was wrong and silently
    under-reported live progress by orders of magnitude. Sums every turn's
    metrics for *session_id* to reconstruct the cumulative total, matching
    the export file's ``final_metrics`` semantics. Opens the db with
    ``?mode=ro`` so concurrent Devin CLI writers are never blocked. Never
    raises.
    """
    if not SESSIONS_DB_PATH.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{SESSIONS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute(
            "SELECT chat_message FROM message_nodes WHERE session_id = ?",
            (session_id,),
        )
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
        found = False
        for (raw,) in cur.fetchall():
            try:
                msg = json.loads(raw)
            except TypeError, json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            meta = msg.get("metadata")
            metrics = meta.get("metrics") if isinstance(meta, dict) else None
            if not isinstance(metrics, dict):
                continue
            totals["input_tokens"] += _coerce_int(metrics.get("input_tokens"))
            totals["output_tokens"] += _coerce_int(metrics.get("output_tokens"))
            totals["cache_read_tokens"] += _coerce_int(metrics.get("cache_read_tokens"))
            totals["cache_write_tokens"] += _coerce_int(metrics.get("cache_creation_tokens"))
            found = True
        if not found or not any(totals.values()):
            return None
        return totals
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _read_devin_usage(export_path: str, session_id: str | None) -> dict[str, int] | None:
    """Cumulative usage for this call: prefer the export file, else sessions.db."""
    usage = _read_export_usage(export_path)
    if usage is not None:
        return usage
    if session_id:
        return _read_sessions_db_usage(session_id)
    return None


def _log_llm_usage(
    *,
    phase: str | None,
    model: str | None,
    usage: dict[str, int] | None,
    duration_s: float,
    agent_id: str | None,
) -> None:
    """Emit an llm.usage RunLog entry — mirrors cursor_backend._log_llm_usage."""
    if not usage:
        return
    run_log = get_run_log()
    if run_log is not None:
        run_log.llm_usage(
            phase=phase,
            model=model,
            duration_s=duration_s,
            agent_id=agent_id,
            **{k: int(v) for k, v in usage.items() if k in _RUN_LOG_USAGE_FIELDS},
        )


def _harvest_devin_usage(
    *,
    cumulative: dict[str, int] | None,
    session_id: str | None,
    phase: str | None,
    model: str | None,
    duration_s: float,
) -> dict[str, int] | None:
    """Diff *cumulative* (already read by ``call_devin``) against the last-seen
    totals for *session_id*, log the delta to RunLog, and return it.

    Returns ``None`` — quietly, without raising — whenever usage can't be
    resolved or the delta is all-zero (e.g. a retry that resumed but made no
    new model calls).
    """
    if cumulative is None:
        return None
    try:
        key = session_id or "unknown"
        previous = _last_session_usage.get(key, {})
        delta = {k: max(v - previous.get(k, 0), 0) for k, v in cumulative.items()}
        _last_session_usage[key] = cumulative
        if not any(delta.values()):
            return None
        _log_llm_usage(
            phase=phase, model=model, usage=delta, duration_s=duration_s, agent_id=session_id
        )
        return delta
    except Exception as exc:  # usage harvesting must never break a run
        logger.debug("devin usage: harvest failed for session=%s: %s", session_id, exc)
        return None


def _progress_poll_loop(
    *,
    work_dir: str,
    export_path: str,
    session_id: str | None,
    started_at: float,
    interval: float,
    stop_event: threading.Event,
    on_progress: Callable[[str | None, dict[str, int]], None],
) -> None:
    """Poll for live usage every *interval* seconds while a devin call is in flight.

    Runs on a daemon thread started by ``call_devin`` for the duration of one
    subprocess attempt. Devin's ``--export`` file is only written once, at
    the very end of a turn (verified live), so for a fresh session this
    mostly reads sessions.db, which Devin updates per assistant turn while a
    multi-step run is still in progress. *on_progress* is called with the
    resolved session id (by cwd — the newest ``sessions`` row whose
    ``working_directory`` matches *work_dir* — or ``None`` if it hasn't been
    resolved yet) alongside the usage dict, so callers can attribute
    mid-run ``usage.progress`` events to the right session instead of
    logging ``agent_id: null``. Never raises: an exception here would
    otherwise silently kill the daemon thread mid-run.
    """
    current_session_id = session_id
    while not stop_event.wait(interval):
        try:
            if current_session_id is None:
                current_session_id = _resolve_live_session_id(
                    work_dir=work_dir, since_ts=started_at
                )
            usage = _read_export_usage(export_path)
            if usage is None and current_session_id:
                usage = _read_sessions_db_usage(current_session_id)
            if usage:
                on_progress(current_session_id, usage)
        except Exception:
            logger.debug("devin progress poll failed", exc_info=True)


def call_devin(
    prompt: str,
    *,
    work_dir: str,
    timeout_s: int = 1800,
    model: str = DEFAULT_MODEL,
    devin_bin: str | None = None,
    mode: str | None = None,
    session_id: str | None = None,
    resume: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    on_progress: Callable[[str | None, dict[str, int]], None] | None = None,
    progress_interval_s: float = _PROGRESS_POLL_INTERVAL_S,
) -> tuple[str, str | None, dict[str, int] | None, int]:
    """Run ``devin --prompt-file ... -p`` with retry/backoff.

    Returns ``(final_text, session_id, usage, exit_code)`` on success
    (exit_code is always ``0`` for a returned result; *usage* is the
    cumulative-for-this-session totals read from the export file, or the
    sessions.db fallback, or ``None`` if neither has usage yet — see
    ``_read_devin_usage``). Raises ``RuntimeError`` — whose message contains
    the failure classification — once retries are exhausted or a
    non-retryable failure occurs.

    On any retry after a partial run, resumes the session id captured from
    the previous attempt's ``--export`` file (``-r <id>``) rather than
    restarting from scratch. Total wall time across all attempts is bounded
    by *timeout_s*; each subprocess call is given the remaining budget, and
    a ``subprocess.TimeoutExpired`` is classified as ``"timeout"``.

    If *on_progress* is given, a background daemon thread polls for live
    usage every *progress_interval_s* seconds for the duration of each
    subprocess attempt and invokes it with ``(session_id, cumulative_usage)``
    — see ``_progress_poll_loop``.
    """
    run_fn = runner or subprocess.run
    bin_path = devin_bin or _find_devin_bin()
    budget = timeout_s if timeout_s > 0 else 1800
    deadline = time.monotonic() + budget
    max_retries = _env_int("DEVIN_MAX_RETRIES", _MAX_RETRIES)
    cooldown = _env_float("DEVIN_RATE_LIMIT_COOLDOWN_S", _RATE_LIMIT_COOLDOWN_S)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".devin-prompt.txt",
        delete=False,
    ) as handle:
        handle.write(prompt)
        prompt_path = handle.name
    export_path = f"{prompt_path}.export.json"

    current_session_id = session_id
    should_resume = resume
    last_classification: str | None = None
    last_err_text = ""
    # Tracks the shared rate-limit deadline *this* call has already extended
    # (and already backed off for via its own retry sleep below), so its own
    # next attempt doesn't wait a second time on a deadline it caused itself —
    # only a deadline pushed further out by another concurrent session.
    own_rate_limit_deadline = 0.0

    try:
        for attempt in range(max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_classification = last_classification or "timeout"
                last_err_text = last_err_text or "devin call exceeded the timeout budget"
                break

            # Honor a cooldown set by another concurrent session in this
            # process before spending an attempt (see _extend_shared_rate_limit).
            _await_shared_rate_limit(sleep, already_satisfied_until=own_rate_limit_deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_classification = last_classification or "rate_limit"
                last_err_text = last_err_text or (
                    "devin call exceeded the timeout budget waiting on a shared rate-limit cooldown"
                )
                break

            cmd = [bin_path]
            if current_session_id and should_resume:
                cmd.extend(["-r", current_session_id])
            cmd.extend(
                [
                    "--prompt-file",
                    prompt_path,
                    "-p",
                    "--model",
                    model,
                    "--respect-workspace-trust",
                    "false",
                    "--export",
                    export_path,
                ]
            )

            env = os.environ.copy()
            # Belt-and-suspenders alongside the --model flag: also set
            # DEVIN_MODEL so the model is unambiguous even if argv parsing
            # (e.g. combined with -r on a resume) ever drops the flag.
            env["DEVIN_MODEL"] = model
            if mode == "plan":
                # Plan mode must not auto-approve workspace edits: leave
                # DEVIN_PERMISSION_MODE unset so the CLI default (read-only
                # auto-approve) applies.
                env.pop("DEVIN_PERMISSION_MODE", None)
            else:
                env["DEVIN_PERMISSION_MODE"] = "bypass"

            progress_thread: threading.Thread | None = None
            stop_progress = threading.Event()
            if on_progress is not None:
                progress_thread = threading.Thread(
                    target=_progress_poll_loop,
                    kwargs={
                        "work_dir": work_dir,
                        "export_path": export_path,
                        "session_id": current_session_id,
                        "started_at": time.time(),
                        "interval": progress_interval_s,
                        "stop_event": stop_progress,
                        "on_progress": on_progress,
                    },
                    daemon=True,
                )
                progress_thread.start()

            try:
                try:
                    result = run_fn(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=remaining,
                        cwd=work_dir,
                        env=env,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    last_classification = "timeout"
                    last_err_text = f"devin timed out after {remaining:.0f}s"
                    exported = _read_export_session_id(export_path)
                    if exported:
                        current_session_id = exported
                    if attempt < max_retries:
                        should_resume = bool(current_session_id)
                        sleep(_backoff_delay(attempt))
                        continue
                    break
            finally:
                stop_progress.set()
                if progress_thread is not None:
                    progress_thread.join(timeout=1.0)

            exported = _read_export_session_id(export_path)
            if exported:
                current_session_id = exported

            if result.returncode == 0:
                usage = _read_devin_usage(export_path, current_session_id)
                return (result.stdout or "").strip(), current_session_id, usage, 0

            text_out = f"{result.stdout or ''}\n{result.stderr or ''}"
            classification = classify_devin_error(text_out)
            last_classification = classification
            last_err_text = text_out.strip()

            if classification in ("rate_limit", "quota"):
                own_rate_limit_deadline = _extend_shared_rate_limit(cooldown)

            if classification in _RETRYABLE_CLASSIFICATIONS and attempt < max_retries:
                should_resume = bool(current_session_id)
                min_wait = cooldown if classification in ("rate_limit", "quota") else None
                sleep(_backoff_delay(attempt, min_wait=min_wait))
                continue
            break
    finally:
        Path(prompt_path).unlink(missing_ok=True)
        Path(export_path).unlink(missing_ok=True)

    classification_label = last_classification or "error"
    raise RuntimeError(f"devin failed ({classification_label}): {last_err_text[:500]}")


@dataclass(frozen=True)
class DevinLLMResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _scope_note(allowed_tools: list[str] | None) -> str:
    if not allowed_tools:
        return ""
    scoped = [tool.removeprefix("path:") for tool in allowed_tools if tool.startswith("path:")]
    if not scoped:
        return ""
    return "\n\nHard scope constraint: only modify files under these prefixes: " + ", ".join(scoped)


class DevinSession:
    """Durable devin session: first send is fresh; later sends ``-r <id>``.

    Pass ``session_id=`` to resume an existing (previously captured) session
    from the very first ``send()`` call instead of starting fresh.
    """

    def __init__(
        self,
        *,
        devin_bin: str,
        model: str,
        cwd: Path,
        mode: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._devin_bin = devin_bin
        self._model = model
        self._cwd = cwd
        self._mode = mode
        self._session_id = session_id
        self._started = session_id is not None
        self.agent_id: str | None = session_id

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> DevinLLMResult:
        del max_tokens, expect_mcp_tools, mcp_requirement
        prompt_with_scope = f"{prompt}{_scope_note(allowed_tools)}"
        t0 = time.monotonic()
        run_log = get_run_log()

        def _on_progress(session_id: str | None, usage: dict[str, int]) -> None:
            # Persist as soon as a session id is resolvable — even mid-flight,
            # before this call returns — so a hard-killed fleet process (e.g.
            # SIGTERM) still leaves a resumable session id behind for the next
            # dispatch of this task (see agent_fleet/session_store.py).
            if session_id:
                persist_session_id(str(self._cwd), session_id)
            if run_log is not None:
                total = sum(usage.values())
                run_log.emit(
                    "usage.progress",
                    data={"total_tokens": total, **usage, "agent_id": session_id or self.agent_id},
                )

        try:
            stdout, session_id, cumulative, code = call_devin(
                prompt_with_scope,
                work_dir=str(self._cwd),
                timeout_s=timeout_s if timeout_s > 0 else 1800,
                model=self._model,
                devin_bin=self._devin_bin,
                mode=self._mode,
                session_id=self._session_id,
                resume=self._started,
                on_progress=_on_progress,
            )
            self._started = True
            if session_id:
                self._session_id = session_id
                self.agent_id = session_id
                persist_session_id(str(self._cwd), session_id)
            duration_s = time.monotonic() - t0
            ctx = get_run_context()
            usage = _harvest_devin_usage(
                cumulative=cumulative,
                session_id=self.agent_id,
                phase=ctx.phase if ctx is not None else None,
                model=self._model,
                duration_s=duration_s,
            )
            return DevinLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=code,
                duration_s=duration_s,
                agent_id=self.agent_id,
                usage=usage,
            )
        except Exception as exc:
            return DevinLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
                agent_id=self.agent_id,
            )

    def dispose(self) -> None:
        """Session lives on disk under ~/.local/share/devin."""


class _DevinErrorSession:
    agent_id: str | None = None

    def __init__(self, message: str) -> None:
        self._message = message

    def send(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        allowed_tools: list[str] | None = None,
        expect_mcp_tools: bool = False,
        mcp_requirement: McpRequirement | None = None,
    ) -> DevinLLMResult:
        del prompt, max_tokens, timeout_s, allowed_tools, expect_mcp_tools, mcp_requirement
        return DevinLLMResult(stdout="", stderr=self._message, exit_code=1, duration_s=0.0)

    def dispose(self) -> None:
        pass


class DevinBackend:
    """Run prompts through the Devin CLI (``devin -p``, OAuth subscription auth)."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        devin_bin: str | None = None,
        default_mode: str | None = None,
    ) -> None:
        self.model = model
        self.devin_bin = str(Path(devin_bin).expanduser()) if devin_bin else _find_devin_bin()
        self.default_mode = default_mode

    def create_session(
        self,
        *,
        persona_name: str,  # noqa: ARG002
        cwd: Path,
        mcp_servers: Mapping[str, McpServerSpec] | None = None,  # noqa: ARG002
        model: str | None = None,
        mode: AgentMode | str | None = None,
        session_id: str | None = None,
    ) -> DevinSession | _DevinErrorSession:
        """Create a devin session. Pass ``session_id`` to resume a previously
        captured session (e.g. carried across a redispatch of an interrupted
        run — see ``agent_fleet/session_store.py``) instead of starting fresh.
        """
        ok, detail, fix = check_devin_auth()
        if not ok:
            msg = detail if not fix else f"{detail}; {fix}"
            return _DevinErrorSession(msg)
        return DevinSession(
            devin_bin=self.devin_bin,
            model=model or self.model,
            cwd=cwd,
            session_id=session_id,
            mode=mode or self.default_mode,
        )

    def run(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout_s: int,
        memory_limit: str = "4G",
        allowed_tools: list[str] | None = None,
        cwd: Path | None = None,
        model: str | None = None,
        mode: str | None = None,
    ) -> DevinLLMResult:
        del max_tokens, memory_limit
        ok, detail, fix = check_devin_auth()
        if not ok:
            msg = detail if not fix else f"{detail}; {fix}"
            return DevinLLMResult(stdout="", stderr=msg, exit_code=1, duration_s=0.0)

        work_dir = str(cwd or Path.cwd())
        selected_model = model or self.model
        selected_mode = mode or self.default_mode
        prompt_with_scope = f"{prompt}{_scope_note(allowed_tools)}"
        t0 = time.monotonic()
        run_log = get_run_log()

        def _on_progress(session_id: str | None, usage: dict[str, int]) -> None:
            if run_log is None:
                return
            total = sum(usage.values())
            run_log.emit(
                "usage.progress",
                data={"total_tokens": total, **usage, "agent_id": session_id},
            )

        try:
            stdout, session_id, cumulative, code = call_devin(
                prompt_with_scope,
                work_dir=work_dir,
                timeout_s=timeout_s if timeout_s > 0 else 1800,
                model=selected_model,
                devin_bin=self.devin_bin,
                mode=selected_mode,
                on_progress=_on_progress if run_log is not None else None,
            )
            duration_s = time.monotonic() - t0
            ctx = get_run_context()
            usage = _harvest_devin_usage(
                cumulative=cumulative,
                session_id=session_id,
                phase=ctx.phase if ctx is not None else None,
                model=selected_model,
                duration_s=duration_s,
            )
            return DevinLLMResult(
                stdout=stdout,
                stderr="",
                exit_code=code,
                duration_s=duration_s,
                agent_id=session_id,
                usage=usage,
            )
        except Exception as exc:
            return DevinLLMResult(
                stdout="",
                stderr=str(exc),
                exit_code=1,
                duration_s=time.monotonic() - t0,
            )
