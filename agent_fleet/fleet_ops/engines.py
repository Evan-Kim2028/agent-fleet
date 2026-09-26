"""Engine invocation: ``cmd`` and ``devin``.

Two engines, each ported from the bash drivers' behaviour rather than their
shell:

**cmd** (``fbrun``)
    Headless ``cmd -p`` on the policy model. The JSONL stream is written to a
    file so it can be judged for tool activity after the fact, and the run is
    classified as a lazy exit (see :mod:`agent_fleet.fleet_ops.lazyexit`)
    because a zero exit with no tool calls is a silent failure.

**devin** (``devin_finish.sh`` / ``xlane``)
    The Devin CLI via the existing :class:`DevinBackend`, plus the two
    behaviours the bash drivers had hand-rolled:

    * **capacity fallback** — a capacity error walks *down* the model ladder
      (``swe-2-high`` → ``swe-2-medium``) before the lane gives up, since the
      high tier is frequently out of capacity while the medium tier is not.
    * **max-output-token auto-continue** — when the run was truncated by the
      output ceiling and there is still no PR, the lane issues **one** continue
      prompt. Exactly one: the bash driver looped up to ten times, which could
      burn hours re-reading the same context.

Both engines launch with ``start_new_session=True`` so the child leads its own
process group. That is what lets ``lanes stop`` terminate precisely one lane
instead of pattern-matching on a command line.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_ops import lazyexit, memcap
from agent_fleet.fleet_ops.fences import render_fences
from agent_fleet.fleet_ops.models import DEVIN_MODEL_LADDER, enforce_implementation_model

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    Runner = Callable[..., "subprocess.CompletedProcess[str]"]

logger = logging.getLogger(__name__)

#: Default per-attempt cap, matching fbrun's ``impl-*`` arm.
DEFAULT_IMPL_TIMEOUT_S = 5 * 3600

#: documents-1d requirement: max-turns default 900 (fbrun used 300 for impl).
DEFAULT_MAX_TURNS = 900

#: Exit 8 is the tool's "turn budget exhausted, resumable" code. documents-1d
#: requires resuming with "continue" on exactly that exit.
RESUME_EXIT_CODE = 8

#: Backoff between devin capacity retries, matching xlane's ``sleep 60``.
CAPACITY_BACKOFF_S = 60.0

#: fbrun's preamble: tell the agent the tree may hold an earlier run's work.
HANDOFF_PREAMBLE = (
    "HANDOFF NOTE: this task may have been started by a previous agent run. If the "
    "worktree has uncommitted changes or unpushed commits, they are that earlier work "
    "on THIS task: inspect them (git status, git diff, git log origin/<branch>..HEAD), "
    "keep what is correct and continue from there. Never git reset --hard / checkout -- "
    "/ clean / stash."
)

#: Signals a run stopped because it hit the output-token ceiling.
TRUNCATION_MARKERS = ("max output token", "truncated", "max_output_tokens")

#: The single continue prompt (ported from devin_finish.sh).
CONTINUE_PROMPT = (
    "Continue the task from where you stopped. Do not re-explore what you already read. "
    "Implement the remaining parts, run the targeted tests, commit, push, and open the PR "
    "with gh pr create. Keep each response short and do the work through tool calls."
)


@dataclass
class EngineResult:
    """What one engine run produced."""

    engine: str
    model: str
    exit_code: int
    final_text: str = ""
    tool_calls: int = 0
    lazy: bool = False
    lazy_reason: str = ""
    stream_path: Path | None = None
    output_path: Path | None = None
    truncated: bool = False
    capacity_fallbacks: list[str] = field(default_factory=list)
    continued: bool = False
    resumes: int = 0
    memory_cap: str = ""
    pid: int | None = None
    pgid: int | None = None
    duration_s: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the run did real work. A lazy exit is never ok."""
        return self.exit_code == 0 and not self.lazy

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "model": self.model,
            "exit_code": self.exit_code,
            "tool_calls": self.tool_calls,
            "lazy": self.lazy,
            "lazy_reason": self.lazy_reason,
            "stream_path": str(self.stream_path) if self.stream_path else None,
            "output_path": str(self.output_path) if self.output_path else None,
            "truncated": self.truncated,
            "capacity_fallbacks": self.capacity_fallbacks,
            "continued": self.continued,
            "resumes": self.resumes,
            "memory_cap": self.memory_cap,
            "duration_s": self.duration_s,
            "detail": self.detail,
        }


def looks_truncated(*texts: str) -> bool:
    """True when any of *texts* mentions an output-token truncation.

    Ported from xlane's ``grep -qiE "truncated|max output token"`` over the
    combined stdout+stderr.
    """
    blob = "\n".join(t for t in texts if t).lower()
    return any(marker in blob for marker in TRUNCATION_MARKERS)


def looks_like_capacity_error(text: str) -> bool:
    """True when a devin failure is a capacity problem (worth retrying lower).

    Ported from ``grep -q "capacity issues"``. Devin's rate-limit and quota
    errors are already handled inside ``call_devin``'s own retry/backoff; this
    catches the distinct capacity exhaustion, which the model ladder addresses.
    """
    return "capacity" in (text or "").lower()


def build_prompt(
    task_text: str,
    *,
    lane: str | None = None,
    branch: str | None = None,
    extra_fences: tuple[str, ...] = (),
) -> str:
    """The implementer prompt: handoff note, lane identity, fences, task text.

    The handoff preamble is what stops a resumed lane from wiping its own
    earlier work — the same protection fbrun's ``pre`` variable provided. The
    fences come *before* the task text (as in fbrun) so the task is read as
    operating under them, not as replacing them.

    *extra_fences* is appended to the house rules, never substituted for them.
    """
    parts = [HANDOFF_PREAMBLE, ""]
    if lane or branch:
        parts.append(f"Lane: {lane or '(unnamed)'}")
        if branch:
            parts.append(f"Branch: {branch}")
        parts.append("")
    parts.append(render_fences(extra_fences))
    parts.append("")
    parts.append(task_text)
    return "\n".join(parts)


def read_task_file(path: Path | str) -> str:
    """Read a task file. Raises ``FileNotFoundError`` with a clear message."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"task file does not exist: {p}")
    return p.read_text(encoding="utf-8")


def _spawn_capture(
    argv: Sequence[str],
    *,
    workdir: Path,
    stream_path: Path | None,
    timeout_s: int,
    runner: Runner | None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, int | None, int | None]:
    """Run *argv* and return ``(exit_code, stdout, stderr, pid, pgid)``.

    With no injected *runner* this spawns for real with
    ``start_new_session=True`` so the child leads its own process group — that is
    what lets ``lanes stop`` signal exactly one lane. When *stream_path* is given,
    stdout is written there as well, because the cmd JSONL stream is what the
    lazy-exit and stall judgements read.

    *env* is the child's complete environment (``None`` inherits the manager's).
    It exists so a caller can put something on the child's ``PATH`` — the
    admission shim in :mod:`agent_fleet.fleet_ops.admission` — which is not
    otherwise reachable, since a parent's ``PATH`` change cannot affect a process
    it has already started.
    """
    if runner is not None:
        result = runner(
            list(argv), cwd=workdir, capture_output=True, text=True, check=False, timeout=timeout_s
        )
        if stream_path is not None:
            stream_path.write_text(result.stdout or "", encoding="utf-8")
        return result.returncode, result.stdout or "", result.stderr or "", None, None

    if stream_path is not None:
        with stream_path.open("w", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                list(argv),
                cwd=workdir,
                env=env,
                stdout=handle,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            _, stderr = proc.communicate()
        stdout = stream_path.read_text(encoding="utf-8", errors="replace")
        return proc.returncode, stdout, stderr or "", proc.pid, _child_pgid(proc.pid)

    proc = subprocess.run(
        list(argv),
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
        start_new_session=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or "", None, None


def run_cmd_engine(
    *,
    workdir: Path,
    prompt: str,
    model: str | None = None,
    run_dir: Path | str,
    name: str = "impl",
    timeout_s: int = DEFAULT_IMPL_TIMEOUT_S,
    max_turns: int = DEFAULT_MAX_TURNS,
    cmd_bin: str | None = None,
    runner: Runner | None = None,
    extra_args: Sequence[str] = (),
    memory_max: str = memcap.DEFAULT_MEMORY_MAX,
    use_systemd: bool | None = None,
    max_resumes: int = 1,
    pr_exists: Callable[[], bool] | None = None,
    env: dict[str, str] | None = None,
) -> EngineResult:
    """Run the ``cmd`` engine headless and judge the result for a lazy exit.

    *model* is validated against the lane policy: an out-of-policy model raises
    before any subprocess starts, so a stray ``FB_MODEL`` in the environment
    cannot redirect a lane.

    The run is memory-capped (owner fence: 6G for anything that runs tests), and
    an exit-8 turn-budget exhaustion gets *one* ``continue`` — the resume
    documents-1d asked for, and the same single-attempt discipline devin gets.

    *env* is the child's complete environment. It is how the admission shim
    reaches the engine (see :func:`agent_fleet.fleet_ops.admission.shim_env`);
    ``None`` inherits the manager's environment unchanged.
    """
    selected = enforce_implementation_model("cmd", model)
    run_path = Path(run_dir)
    run_path.mkdir(parents=True, exist_ok=True)
    stream_path = run_path / f"{name}.jsonl"
    output_path = run_path / f"{name}.out"

    binary = cmd_bin or "cmd"
    args = [
        binary,
        "-p",
        prompt,
        "-m",
        selected,
        "--yolo",
        "-t",
        "--skip-onboarding",
        "--no-auto-update",
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        "--tools-all",
        *extra_args,
    ]
    plan = memcap.plan_memory_cap(args, memory_max=memory_max, use_systemd=use_systemd)

    started = time.monotonic()
    exit_code, stream_text, stderr, pid, pgid = _spawn_capture(
        plan.argv,
        workdir=workdir,
        stream_path=stream_path,
        timeout_s=timeout_s,
        runner=runner,
        env=env,
    )

    resumes = 0
    if exit_code == RESUME_EXIT_CODE and not (pr_exists and pr_exists()):
        for _ in range(max(0, max_resumes)):
            logger.info("cmd exited %s (turn budget); one continue", RESUME_EXIT_CODE)
            cont_args = [
                binary,
                "-p",
                CONTINUE_PROMPT,
                "-m",
                selected,
                "--yolo",
                "-t",
                "--skip-onboarding",
                "--no-auto-update",
                "--output-format",
                "json",
                "--max-turns",
                str(max_turns),
                "--tools-all",
                *extra_args,
            ]
            cont_plan = memcap.plan_memory_cap(
                cont_args, memory_max=memory_max, use_systemd=use_systemd
            )
            # Same spawn helper as the first attempt: the resume must also lead
            # its own process group, or `lanes stop` would not reach it.
            cont_exit, cont_stdout, cont_stderr, cont_pid, cont_pgid = _spawn_capture(
                cont_plan.argv,
                workdir=workdir,
                stream_path=None,
                timeout_s=timeout_s,
                runner=runner,
                env=env,
            )
            resumes += 1
            stream_text = f"{stream_text}\n{cont_stdout}"
            exit_code = cont_exit
            stderr = cont_stderr or stderr
            pgid = cont_pgid if cont_pgid is not None else pgid
            pid = cont_pid if cont_pid is not None else pid
            if exit_code != RESUME_EXIT_CODE or (pr_exists and pr_exists()):
                break

    duration = time.monotonic() - started
    verdict = lazyexit.judge_run(exit_code=exit_code, stream_text=stream_text)
    final_text = lazyexit.extract_final_text(stream_text)
    output_path.write_text(final_text, encoding="utf-8")

    return EngineResult(
        engine="cmd",
        model=selected,
        exit_code=verdict.exit_code,
        final_text=final_text,
        tool_calls=verdict.tool_calls,
        lazy=verdict.lazy,
        lazy_reason=verdict.reason,
        stream_path=stream_path,
        output_path=output_path,
        resumes=resumes,
        memory_cap=plan.memory_max,
        pid=pid,
        pgid=pgid,
        duration_s=duration,
        detail=(stderr or "").strip()[:2000],
    )


def _child_pgid(pid: int) -> int | None:
    """Best-effort pgid of a just-exited child (``start_new_session`` makes it the leader)."""
    try:
        import os

        return os.getpgid(pid)
    except ProcessLookupError, PermissionError, OSError:
        # The child is gone; with start_new_session it led its own group whose
        # id equals its pid.
        return pid


def run_devin_engine(
    *,
    workdir: Path,
    prompt: str,
    model: str | None = None,
    run_dir: Path | str | None = None,
    name: str = "impl",
    timeout_s: int = DEFAULT_IMPL_TIMEOUT_S,
    devin_bin: str | None = None,
    runner: Runner | None = None,
    max_continues: int = 1,
    pr_exists: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    env: dict[str, str] | None = None,
) -> EngineResult:
    """Run the devin engine with capacity fallback and one auto-continue.

    The ladder is walked only on a *capacity* error. A truncated run that still
    has no PR gets exactly ``max_continues`` continue attempts. If a PR exists
    by then, the continue is skipped entirely — there is nothing left to do.

    *run_dir* mirrors :func:`run_cmd_engine`: the per-attempt output is written
    there and recorded on the result, so a devin lane leaves the same auditable
    artifact trail the cmd engine does.

    *env* is the child's complete environment, the same admission seam
    :func:`run_cmd_engine` exposes; ``None`` inherits the manager's.
    """
    selected = model or DEVIN_MODEL_LADDER[0]
    # Validate against policy without pinning devin to a single model: the
    # ladder is a policy-sanctioned fallback, so only reject foreign models.
    if selected not in DEVIN_MODEL_LADDER:
        from agent_fleet.fleet_ops.models import ModelPolicyError

        raise ModelPolicyError(
            f"devin is restricted to the model ladder {list(DEVIN_MODEL_LADDER)}; "
            f"refusing to run {selected!r}"
        )

    run_path = Path(run_dir) if run_dir is not None else None
    output_path: Path | None = None
    if run_path is not None:
        run_path.mkdir(parents=True, exist_ok=True)
        output_path = run_path / f"{name}.out"

    ladder: list[str] = []
    last_result: EngineResult | None = None
    for index, candidate in enumerate(DEVIN_MODEL_LADDER):
        ladder.append(candidate)
        result = _devin_attempt(
            workdir=workdir,
            prompt=prompt,
            model=candidate,
            name=name,
            timeout_s=timeout_s,
            devin_bin=devin_bin,
            runner=runner,
            env=env,
        )
        last_result = result
        if not looks_like_capacity_error(result.detail):
            break
        if index < len(DEVIN_MODEL_LADDER) - 1:
            # xlane slept 60s between capacity retries. Back off before
            # descending a rung, but never after the last one.
            logger.info("devin capacity error on %s; descending the ladder", candidate)
            sleep(CAPACITY_BACKOFF_S)

    assert last_result is not None
    result = last_result
    result.capacity_fallbacks = ladder[1:] if len(ladder) > 1 else []

    combined = f"{result.final_text}\n{result.detail}"
    result.truncated = looks_truncated(combined)

    if result.truncated and (pr_exists is None or not pr_exists()):
        for _ in range(max(0, max_continues)):
            if pr_exists is not None and pr_exists():
                break
            logger.info("devin run truncated by the output ceiling; issuing one continue")
            cont = _devin_attempt(
                workdir=workdir,
                prompt=CONTINUE_PROMPT,
                model=result.model,
                name=f"{name}-continue",
                timeout_s=timeout_s,
                devin_bin=devin_bin,
                runner=runner,
                env=env,
            )
            result.continued = True
            result.final_text = f"{result.final_text}\n{cont.final_text}".strip()
            result.detail = cont.detail
            result.tool_calls += cont.tool_calls
            result.exit_code = cont.exit_code
            if not looks_truncated(f"{cont.final_text}\n{cont.detail}"):
                break

    if output_path is not None:
        output_path.write_text(result.final_text, encoding="utf-8")
        result.output_path = output_path

    return result


def _devin_attempt(
    *,
    workdir: Path,
    prompt: str,
    model: str,
    name: str,
    timeout_s: int,
    devin_bin: str | None,
    runner: Runner | None,
    env: dict[str, str] | None = None,
) -> EngineResult:
    """One devin attempt through the existing backend."""
    from agent_fleet.devin_backend import DevinBackend

    if runner is not None:
        # The injected runner owns its own spawning, so there is no environment
        # for this module to hand it — `env` applies to the real path below.
        return _devin_attempt_subprocess(
            workdir=workdir,
            prompt=prompt,
            model=model,
            name=name,
            timeout_s=timeout_s,
            devin_bin=devin_bin,
            runner=runner,
        )

    backend = DevinBackend(model=model, devin_bin=devin_bin)
    started = time.monotonic()
    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=timeout_s,
        cwd=workdir,
        runner=runner,
        env=env,
    )
    return EngineResult(
        engine="devin",
        model=model,
        exit_code=result.exit_code,
        final_text=result.stdout or "",
        memory_cap=memcap.DEVIN_MEMORY_MAX,
        detail=(result.stderr or "").strip()[:2000],
        duration_s=time.monotonic() - started,
    )


def _devin_attempt_subprocess(
    *,
    workdir: Path,
    prompt: str,
    model: str,
    name: str,  # noqa: ARG001
    timeout_s: int,
    devin_bin: str | None,
    runner: Runner,
) -> EngineResult:
    """Devin attempt via an injected runner (tests, and callers that own spawning)."""
    binary = devin_bin or "devin"
    args = [binary, "-p", "--model", model, "--respect-workspace-trust", "false", "--", prompt]
    started = time.monotonic()
    result = runner(
        args, cwd=workdir, capture_output=True, text=True, check=False, timeout=timeout_s
    )
    return EngineResult(
        engine="devin",
        model=model,
        exit_code=result.returncode,
        final_text=(result.stdout or "").strip(),
        detail=(result.stderr or "").strip()[:2000],
        duration_s=time.monotonic() - started,
    )
