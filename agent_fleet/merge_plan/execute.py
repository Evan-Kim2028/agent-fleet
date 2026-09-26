"""Execute the batches :mod:`agent_fleet.merge_plan` plans.

The planner decides *what* ships together; this module ships it.  It runs as a
tick -- collect approvals, plan, then merge -> deploy -> verify each eligible
batch -- so it is safe to drive from cron, a CI job, or ``--daemon``.

Two rules shape the design:

**Nothing is remembered that GitHub already knows.**  Every tick re-reads live
PR state and decides from that.  A hand-maintained "already merged" list
seeded with in-flight lanes once stranded twelve approved PRs, because the
list and reality disagreed.  Here the only reasons to skip a PR are live facts:
it is merged, its head moved, it is conflicting, or it is held.

**A lock is held by a live process, never by a file that can outlive it.**  A
merge script that exited on a conflict without releasing a bare marker file
deadlocked the next repository for an hour and forty minutes.  :class:`DeployLock`
holds an ``flock`` on an open descriptor, which the kernel releases the moment
the process dies, whatever the exit path.  The sidecar ``.meta`` record exists
only so a human -- and the reclaim path -- can tell *who* holds it, and is
reclaimed when its holder is provably gone.

The executor holds no repository knowledge.  Every command it runs comes from
``merge_plan.repos[].{merge,deploy,verify}_template`` in fleet.yaml; a repo
without them is reported as unconfigured rather than guessed at.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent_fleet.merge_plan.batching import render_executor_commands
from agent_fleet.merge_plan.collect import GitHubClient, _scoped_client
from agent_fleet.merge_plan.config import load_executor_spec, resolve_repo_specs
from agent_fleet.merge_plan.plan import build_plan
from agent_fleet.merge_plan.types import (
    DEFAULT_MAX_BATCH_SIZE,
    ApprovedPR,
    Batch,
    ClusterHold,
    ExecutorSpec,
    MergePlan,
    RepoSpec,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import IO


class PRReader(Protocol):
    """The GitHub surface the executor uses.

    Declared as a protocol so a test can pass a small stand-in with two
    methods, while production passes the real ``GitHubClient`` -- including
    into the planner, which takes that concrete type.  ``for_repo`` is part of
    the contract because ``gh pr view <n>`` resolves against the origin remote
    of the checkout it runs in, so a client must be re-scopable per repository.
    """

    def pr_detail(self, pr_number: int) -> dict[str, Any]: ...

    def for_repo(self, repo_path: Path | None) -> PRReader: ...


class EventSink(Protocol):
    """The ``RunLog.emit`` shape, narrowed to what the executor calls."""

    def emit(
        self, event: str, *, level: str = ..., data: dict[str, Any] | None = ...
    ) -> object: ...


#: Exit code a command reports when it times out, so it is distinguishable from
#: the command's own failure codes.
TIMEOUT_EXIT_CODE = 124

#: GitHub's transient "still computing" value. Treated as unknown, never as a
#: green light: the PR is left for the next tick to judge.
MERGEABLE_UNKNOWN = "UNKNOWN"

_LOCK_LEASE_NAME = "boot_id"
_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """The result of one merge/deploy/verify/rebase command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class BatchOutcome:
    """What one tick did with one batch.

    ``status`` is the single word a reader needs: ``merged`` shipped, ``held``
    a policy said no, ``locked`` another live process is already deploying this
    repo, ``needs_rebase`` its PRs conflict, ``failed`` a command failed.
    """

    index: int
    repo: str
    status: str
    detail: str = ""
    prs: tuple[int, ...] = ()
    needs_rebase: tuple[int, ...] = ()
    merged_sha: str = ""
    commands: tuple[str, ...] = ()
    lane: str = ""

    @property
    def pr_numbers(self) -> tuple[int, ...]:
        """Alias for :attr:`prs`, so an outcome reads naturally either way."""
        return self.prs

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "repo": self.repo,
            "status": self.status,
            "detail": self.detail,
            "prs": list(self.prs),
            "needs_rebase": list(self.needs_rebase),
            "merged_sha": self.merged_sha,
            "commands": list(self.commands),
            "lane": self.lane,
        }


@dataclass(frozen=True)
class TickResult:
    """The whole tick: per-batch outcomes, events emitted, and totals."""

    outcomes: tuple[BatchOutcome, ...] = ()
    events: tuple[str, ...] = ()
    run_id: str = ""

    def by_status(self, status: str) -> tuple[BatchOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "batches": len(self.outcomes),
            "merged": len(self.by_status("merged")),
            "held": len(self.by_status("held")),
            "locked": len(self.by_status("locked")),
            "needs_rebase": len(self.by_status("needs_rebase")),
            "failed": len(self.by_status("failed")),
            "skipped": len(self.by_status("skipped")),
            "outcomes": [o.to_dict() for o in self.outcomes],
            "events": list(self.events),
        }

    def render_text(self) -> str:
        """A short operator-facing summary of the tick."""
        if not self.outcomes:
            return "merge run: no batches planned"
        lines = [f"merge run: {len(self.outcomes)} batch(es)"]
        for outcome in self.outcomes:
            marker = {
                "merged": "OK",
                "held": "HELD",
                "locked": "BUSY",
                "needs_rebase": "REBASE",
                "failed": "FAIL",
                "skipped": "skip",
            }.get(outcome.status, outcome.status)
            prs = ",".join(str(p) for p in outcome.prs) or "-"
            detail = f"  {outcome.detail}" if outcome.detail else ""
            lines.append(f"  [{marker}] {outcome.repo} #{prs} (batch {outcome.index}){detail}")
        summary = self.to_dict()
        lines.append(
            f"  {summary['merged']} merged, {summary['held']} held, "
            f"{summary['locked']} busy, {summary['needs_rebase']} need rebase, "
            f"{summary['failed']} failed, {summary['skipped']} skipped"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deploy lock
# ---------------------------------------------------------------------------


def _boot_id() -> str | None:
    """This host's boot id, or ``None`` where ``/proc`` is unavailable.

    Two locks written before and after a reboot must not be confused: the pid
    can be reused, but the boot id cannot.
    """
    try:
        return _BOOT_ID_PATH.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _proc_start_time(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat``: when *pid* started, in boot ticks.

    This is what distinguishes "pid 4812 is alive" from "pid 4812 is alive and
    is still the process that took this lock".  The kernel recycles pids, and a
    recycled pid would otherwise pin a stale lock forever.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # The comm field is parenthesised and may itself contain spaces and
    # parentheses, so split only after its final ')'.
    close = raw.rfind(")")
    if close == -1:
        return None
    fields = raw[close + 2 :].split()
    # fields[0] is stat field 3 (state), so stat field 22 is index 19.
    return fields[19] if len(fields) > 19 else None


def _pid_alive(pid: int) -> bool:
    """Signal-0 liveness probe for a single pid. Sends nothing, kills nothing."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by another user.
        return True
    except OSError:
        return False
    return True


class DeployLock:
    """An exclusive, process-held claim on one repository's deploy slot.

    Acquired with ``flock(LOCK_EX | LOCK_NB)`` on an open descriptor.  The lock
    belongs to that descriptor, so the kernel drops it when the process exits --
    normally, on an exception, on ``KeyboardInterrupt``, or on a crash.  There
    is no exit path that can leak it, which is precisely what a bare marker
    file cannot promise.

    A sidecar ``<name>.meta`` records who holds it, for humans and for the
    reclaim path.  It is advisory: it never grants or denies the lock, it only
    explains it.
    """

    def __init__(self, path: Path, *, repo: str = "", pid: int | None = None) -> None:
        self.path = path
        self.repo = repo
        self._pid = pid if pid is not None else os.getpid()
        self._fd: int | None = None

    @property
    def meta_path(self) -> Path:
        return self.path.with_suffix(".meta")

    @property
    def held(self) -> bool:
        return self._fd is not None

    def read_meta(self) -> dict[str, Any]:
        """The recorded holder, or ``{}`` when absent or unreadable."""
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def write_meta(self) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": self._pid,
            _LOCK_LEASE_NAME: _boot_id(),
            "pid_start_time": _proc_start_time(self._pid),
            "repo": self.repo,
            "acquired_ts": time.time(),
        }
        self.meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def clear_meta(self) -> None:
        """Remove the sidecar. Only the holder calls this, after it released."""
        with contextlib.suppress(OSError):
            self.meta_path.unlink()

    def holder_is_alive(self) -> bool:
        """Whether the recorded holder is still running as the same process.

        False when the record names a dead pid, a pid the kernel has recycled,
        or a lock taken before the last reboot.  Where ``/proc`` is absent this
        degrades to a bare liveness probe rather than guessing.
        """
        meta = self.read_meta()
        pid = meta.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False

        recorded_boot = meta.get(_LOCK_LEASE_NAME)
        current_boot = _boot_id()
        if recorded_boot and current_boot and recorded_boot != current_boot:
            # The machine rebooted under this lock; nothing it recorded survives.
            return False

        if not _pid_alive(pid):
            return False

        recorded_start = meta.get("pid_start_time")
        current_start = _proc_start_time(pid)
        if recorded_start and current_start:
            return recorded_start == current_start
        return True

    def try_acquire(self) -> bool:
        """Take the lock without blocking. False when a live process holds it.

        A live holder is never displaced, even if its meta looks wrong: the
        kernel says the lock is taken, and taking it anyway would reintroduce
        exactly the overlapping deploy this exists to prevent.  A leftover meta
        whose holder is provably gone is simply reclaimed once we hold the
        descriptor.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # EWOULDBLOCK: another live process holds this descriptor's lock.
            os.close(fd)
            return False
        self._fd = fd
        if self.read_meta() and not self.holder_is_alive():
            # Unreachable in practice -- the kernel would have released the
            # flock with the dead holder -- but reclaiming keeps a stale record
            # from misreporting who is deploying.
            self.clear_meta()
        self.write_meta()
        return True

    def release(self) -> None:
        """Drop the lock and remove the sidecar. Idempotent."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)
        self.clear_meta()

    def __enter__(self) -> DeployLock:
        if not self.try_acquire():
            raise BlockingIOError(f"deploy lock held: {self.path}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def deploy_lock_for(state_dir: Path, repo: str) -> DeployLock:
    """The lock guarding *repo*'s deploy slot under *state_dir*."""
    return DeployLock(state_dir / f"deploy-{repo}.lock", repo=repo)


# ---------------------------------------------------------------------------
# Hold ledger
# ---------------------------------------------------------------------------


class HoldLedger:
    """Durable executor state: which cluster holds are released, and who ran last.

    Deliberately *not* a record of which PRs have merged -- that is GitHub's
    job, and duplicating it here is how a hand-seeded list went stale and
    stranded a queue.  This file holds only operator intent (releases) and
    scheduling bookkeeping (fairness).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def released_holds(self) -> set[str]:
        raw = self._read().get("released_holds")
        return {str(v) for v in raw} if isinstance(raw, list) else set()

    def active_holds(self, spec: ExecutorSpec) -> tuple[ClusterHold, ...]:
        """Holds from config that have not been released."""
        released = self.released_holds()
        return tuple(h for h in spec.holds if h.name not in released)

    def release(self, name: str) -> bool:
        """Mark *name* released. False when it was not currently released."""
        data = self._read()
        raw = data.get("released_holds")
        released = [str(v) for v in raw] if isinstance(raw, list) else []
        if name in released:
            return False
        released.append(name)
        data["released_holds"] = sorted(released)
        self._write(data)
        return True

    @staticmethod
    def _group_key(group: Sequence[str]) -> str:
        return "|".join(sorted(group))

    def group_state(self, group: Sequence[str]) -> dict[str, Any]:
        groups = self._read().get("groups")
        if not isinstance(groups, dict):
            return {}
        entry = groups.get(self._group_key(group))
        return entry if isinstance(entry, dict) else {}

    def last_served(self, group: Sequence[str]) -> str:
        return str(self.group_state(group).get("last_served") or "")

    def hold_until(self, group: Sequence[str]) -> float:
        value = self.group_state(group).get("hold_until")
        return float(value) if isinstance(value, int | float) else 0.0

    def record_served(self, *, group: Sequence[str], repo: str, hold_until: float) -> None:
        """Note that *repo* just ran, so the group alternates to its peers next."""
        data = self._read()
        groups = data.get("groups")
        if not isinstance(groups, dict):
            groups = {}
        groups[self._group_key(group)] = {"last_served": repo, "hold_until": hold_until}
        data["groups"] = groups
        self._write(data)

    def pending_deploy(self, batch_key: str) -> dict[str, Any]:
        """The recorded half-shipped batch for *batch_key*, or ``{}``."""
        pending = self._read().get("pending_deploys")
        if not isinstance(pending, dict):
            return {}
        entry = pending.get(batch_key)
        return entry if isinstance(entry, dict) else {}

    def record_pending_deploy(self, *, batch_key: str, repo: str, merge_sha: str) -> None:
        """Record that a batch merged but its deploy did not succeed."""
        data = self._read()
        pending = data.get("pending_deploys")
        if not isinstance(pending, dict):
            pending = {}
        pending[batch_key] = {"repo": repo, "merge_sha": merge_sha, "at": time.time()}
        data["pending_deploys"] = pending
        self._write(data)

    def clear_pending_deploy(self, batch_key: str) -> None:
        """Drop the record once the deploy and verify commands have succeeded."""
        data = self._read()
        pending = data.get("pending_deploys")
        if not isinstance(pending, dict) or batch_key not in pending:
            return
        del pending[batch_key]
        data["pending_deploys"] = pending
        self._write(data)


def load_ledger(spec: ExecutorSpec) -> HoldLedger:
    return HoldLedger(Path(spec.state_dir).expanduser() / "ledger.json")


def release_hold(spec: ExecutorSpec, name: str) -> bool:
    """Clear a named cluster hold. Returns False when it was not released."""
    return load_ledger(spec).release(name)


# ---------------------------------------------------------------------------
# Command rendering and execution
# ---------------------------------------------------------------------------


def render_command(
    template: str,
    *,
    pr_args: str = "",
    pr: str = "",
    sha9: str = "",
    merge_sha: str = "",
    repo: str = "",
    lane: str = "",
) -> str:
    """Substitute the placeholders a command template may use."""
    return (
        template.replace("{pr_args}", pr_args)
        .replace("{pr}", pr)
        .replace("{sha9}", sha9)
        .replace("{merge_sha}", merge_sha)
        .replace("{repo}", repo)
        .replace("{lane}", lane)
    )


def command_argv(template: str, **fields: str) -> list[str]:
    """Render *template* and split it into an argv.

    Split with :func:`shlex.split` and run without a shell, so a template can
    quote its arguments but can never be re-interpreted by a shell.
    """
    return shlex.split(render_command(template, **fields))


#: How long a command's output is still collected after it exits.  A command
#: that backgrounds work leaves a grandchild holding the inherited pipe write
#: ends, so waiting for EOF on them is not bounded by the command's lifetime.
#: The wait is therefore capped, and the ceiling stays the ceiling.
_PIPE_DRAIN_GRACE_SECONDS = 1.0


def _drain_streams(streams: Sequence[IO[str] | None]) -> list[str]:
    """Read every stream to EOF, on a worker thread the caller can abandon."""
    out: list[str] = []
    for stream in streams:
        if stream is None:
            out.append("")
            continue
        try:
            out.append(stream.read())
        except OSError, ValueError:
            out.append("")
    return out


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill the command we started, and nothing else.

    ``start_new_session=True`` gave it its own process group, so this reaches
    the children a merge script backgrounds without ever touching a pid this
    executor did not create.
    """
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError, ProcessLookupError):
        proc.kill()


def _run_command(
    argv: Sequence[str],
    *,
    cwd: str | None,
    timeout: int,
    dry_run: bool,
) -> CommandResult:
    """Run one command, killing only the pid we create if it overruns.

    *cwd* is expanded first, because the documented config form is
    ``path: ~/Documents/<repo>`` and ``Popen`` performs no tilde expansion: the
    raw string fails the spawn and every command returns rc=127.

    The timeout is a real ceiling.  Waiting for the pipe to close would let a
    backgrounded grandchild hold the executor for its whole remaining
    lifetime, so output is collected on a thread that is given a bounded grace
    period and then dropped, while the process itself is reaped immediately.
    """
    args = tuple(argv)
    if dry_run:
        return CommandResult(argv=args, returncode=0, dry_run=True)
    if not args:
        return CommandResult(argv=args, returncode=0)
    try:
        proc = subprocess.Popen(
            list(args),
            cwd=str(Path(cwd).expanduser()) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return CommandResult(argv=args, returncode=127, stderr=str(exc))

    collected: list[str] = []
    reader = threading.Thread(
        target=lambda: collected.extend(_drain_streams([proc.stdout, proc.stderr])),
        daemon=True,
        name="merge-command-output",
    )
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=_PIPE_DRAIN_GRACE_SECONDS)
        reader.join(_PIPE_DRAIN_GRACE_SECONDS)
        return CommandResult(argv=args, returncode=TIMEOUT_EXIT_CODE, timed_out=True)

    reader.join(_PIPE_DRAIN_GRACE_SECONDS)
    stdout, stderr = ([*collected, "", ""])[:2]
    return CommandResult(argv=args, returncode=proc.returncode, stdout=stdout, stderr=stderr)


class _Emitter:
    """Emit merge events through the fleet event path, and remember them.

    Events go to the real ``RunLog`` when one is supplied, so they reach the
    runs-dir JSONL, Logfire, and the in-memory ring exactly like every other
    fleet event.  When there is no run log -- a dry run, or a unit test -- the
    names are still recorded on the tick result so the sequence is assertable.
    """

    def __init__(self, run_log: EventSink | None) -> None:
        self._run_log = run_log
        self.events: list[str] = []

    def __call__(self, event: str, *, level: str = "info", **payload: object) -> None:
        self.events.append(event)
        if self._run_log is None:
            return
        self._run_log.emit(event, level=level, data=dict(payload) or None)


# ---------------------------------------------------------------------------
# Live PR state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PRState:
    """What GitHub says about one PR right now."""

    state: str = ""
    head_sha: str = ""
    mergeable: str = ""

    @property
    def is_merged(self) -> bool:
        return self.state.upper() == "MERGED"

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"

    @property
    def is_conflicting(self) -> bool:
        return self.mergeable.upper() == "CONFLICTING"

    @property
    def is_mergeable(self) -> bool:
        return self.is_open and self.mergeable.upper() == "MERGEABLE"

    def head_moved(self, approved_sha: str) -> bool:
        """Whether the head has moved past the SHA the gate approved.

        An unreadable head is not a moved head: the caller treats it as
        unverifiable and skips, rather than calling it stale.
        """
        if not self.head_sha or not approved_sha:
            return False
        return not self.head_sha.startswith(approved_sha)


def _pr_state(detail: dict[str, Any]) -> _PRState:
    """Read the executor's four fields out of a ``gh pr view`` payload.

    A payload missing ``state`` or ``mergeable`` is treated as *unknown*, not as
    permission to proceed: an unreadable PR is skipped, never assumed safe.
    """
    return _PRState(
        state=str(detail.get("state") or ""),
        head_sha=str(detail.get("headRefOid") or ""),
        mergeable=str(detail.get("mergeable") or ""),
    )


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def _partition_prs(
    prs: Sequence[ApprovedPR],
    *,
    client: PRReader,
    repo_path: Path | None,
) -> tuple[list[ApprovedPR], list[ApprovedPR], list[tuple[ApprovedPR, str]]]:
    """Split *prs* into (mergeable, conflicting, [(skipped, reason)]).

    This is where merge state is derived: from GitHub, every tick, per PR.
    A PR is skipped for a live, checkable reason and never because some list
    says it was handled.
    """
    scoped = _scoped_client(client, repo_path)
    mergeable: list[ApprovedPR] = []
    conflicting: list[ApprovedPR] = []
    skipped: list[tuple[ApprovedPR, str]] = []
    for pr in prs:
        detail = scoped.pr_detail(pr.pr_number)
        if not detail:
            skipped.append((pr, "pr state unreadable"))
            continue
        state = _pr_state(detail)
        if state.is_merged:
            skipped.append((pr, "already merged"))
            continue
        if state.head_moved(pr.approved_sha):
            skipped.append((pr, f"head moved past approved {pr.sha9}"))
            continue
        if not state.is_open:
            skipped.append((pr, f"state {state.state or 'unknown'}"))
            continue
        if state.mergeable.upper() == MERGEABLE_UNKNOWN:
            skipped.append((pr, "mergeability still computing"))
            continue
        if state.is_conflicting:
            conflicting.append(pr)
            continue
        if state.is_mergeable:
            mergeable.append(pr)
            continue
        skipped.append((pr, f"mergeable={state.mergeable or 'unknown'}"))
    return mergeable, conflicting, skipped


def _merge_commands(prs: Sequence[ApprovedPR], repo_spec: RepoSpec, *, lane: str) -> list[str]:
    """The merge command lines for a batch, with every placeholder resolved.

    Wraps :func:`render_executor_commands` so the planner's ``{pr_args}`` shape
    and the per-PR shape are decided in one place, and so a template that also
    uses ``{repo}`` or ``{lane}`` gets those filled in rather than left literal.
    """
    rendered = render_executor_commands(prs, repo_spec)
    return [
        command.replace("{repo}", repo_spec.name).replace("{lane}", lane) for command in rendered
    ]


def _run_rebase(
    pr: ApprovedPR,
    *,
    spec: ExecutorSpec,
    repo_spec: RepoSpec,
    emit: _Emitter,
    dry_run: bool,
) -> str:
    """Hand a conflicting PR to the configured rebase command.

    The rebase pushes a new head, so the PR re-enters the gate naturally on the
    next tick; until a fresh approval lands for that head, the stale-approval
    rule correctly refuses to merge it.  The command is run at most once per
    conflict, so a permanently unresolvable rebase cannot spin the executor.
    """
    template = repo_spec.rebase_template or spec.rebase_command
    if not template:
        emit(
            "merge.needs_rebase",
            level="warning",
            repo=pr.repo,
            pr=pr.pr_number,
            reason="no rebase command configured",
        )
        return ""
    argv = command_argv(
        template,
        pr=str(pr.pr_number),
        sha9=pr.sha9,
        repo=pr.repo,
        lane=pr.lane,
    )
    result = _run_command(
        argv,
        cwd=repo_spec.path or None,
        timeout=spec.command_timeout_seconds,
        dry_run=dry_run,
    )
    emit(
        "merge.needs_rebase",
        level="info" if result.ok else "error",
        repo=pr.repo,
        pr=pr.pr_number,
        lane=pr.lane,
        rc=result.returncode,
        argv=" ".join(argv),
    )
    return " ".join(argv)


class _ServedState:
    """Which repo each exclusive group last served, and which ran this tick.

    Two different questions, deliberately kept apart:

    * *Who went last* (persisted across ticks) is the group's history: it names
      the repo that most recently deployed, and it is what tells an operator
      why a group is where it is.
    * *Who ran in this tick* (in memory only) drives **exclusion** -- two repos
      in one group must never deploy inside the same tick, whatever order the
      plan produced.

    Conflating them deadlocks the group: reading a peer's previous turn as
    "it ran this tick" would hold every repo forever after the first merge,
    which is why the second question is never answered from the first.
    """

    def __init__(self, ledger: HoldLedger) -> None:
        self._ledger = ledger
        self._last: dict[str, str] = {}
        self._hold_until: dict[str, float] = {}
        self._served_this_tick: dict[str, str] = {}
        self._planned: dict[str, set[str]] = {}

    def note_planned(self, groups: Sequence[Sequence[str]], repos: Sequence[str]) -> None:
        """Record which of *repos* each group expects to serve this tick.

        Called once for the whole plan, before the first batch runs, so the
        group is judged on the peers it is *about* to serve and not on the ones
        the plan happened to order first.  A repo that is present but gets held
        by policy still counts as having work waiting, which is what makes the
        turn it is owed worth handing over.
        """
        for group in groups:
            self._planned.setdefault(self._key(group), set()).update(repos)

    def prime(self, groups: Sequence[Sequence[str]]) -> None:
        """Load the persisted state for every group this tick might touch."""
        for group in groups:
            key = self._key(group)
            if key in self._last:
                continue
            self._last[key] = self._ledger.last_served(group)
            self._hold_until[key] = self._ledger.hold_until(group)

    @staticmethod
    def _key(group: Sequence[str]) -> str:
        return HoldLedger._group_key(group)

    def snapshot(self, group: Sequence[str]) -> tuple[float, str]:
        """``(hold_until, last_served)`` for *group* as this tick sees it."""
        key = self._key(group)
        return self._hold_until.get(key, 0.0), self._last.get(key, "")

    def planned_repos(self, group: Sequence[str]) -> set[str]:
        """The group members this tick's plan is ready to serve."""
        return self._planned.get(self._key(group), set())

    def served_this_tick(self, group: Sequence[str]) -> str:
        """The group member that already deployed in this tick, or ``""``."""
        return self._served_this_tick.get(self._key(group), "")

    def record(self, *, group: Sequence[str], repo: str, hold_until: float) -> None:
        key = self._key(group)
        self._last[key] = repo
        self._hold_until[key] = hold_until
        self._served_this_tick[key] = repo


def _group_block(
    batch: Batch,
    *,
    spec: ExecutorSpec,
    served: _ServedState,
    now: Callable[[], float],
) -> BatchOutcome | None:
    """Why *batch* may not deploy right now, or ``None`` when it may.

    The decision is about the **group**, not about the repo asking, so a repo
    can never spend another repo's turn and a group's schedule never depends on
    the order the plan happened to put batches in.  Three rules, all of them
    group-wide:

    * the post-merge quiet period is the group's, and gates every member;
    * two repos of one group never deploy inside the same tick.  A repo is not
      excluded by its own earlier batch: exclusivity is about *peers* sharing a
      deploy surface, and a repo draining its own queue touches nothing else;
    * turn taking -- whoever went last yields.

    The one that took the most care is the third.  A turn can only be handed to
    a peer that is *here*, so the group looks at the plan rather than at the
    order the batches happen to arrive in: a peer listed after the batch asking
    the question still counts as waiting, which is what stops a busy repo from
    spending the whole tick draining its queue ahead of it.  And a peer that is
    absent has no turn to take, so waiting for one is not fairness, it is a
    permanent self-inflicted deadlock -- a repo with a steady approved queue
    would be held on every future tick with nothing ever merging.  Nobody holds
    out for a peer that is not there.

    A batch that breaks more than one rule says so in one line, so a repo held
    by the quiet period and by its peer's turn reads as both rather than as
    whichever rule happened to be checked first.
    """
    repo = batch.repo
    group = spec.group_for(repo)
    if not group:
        return None
    pr_numbers = tuple(p.pr_number for p in batch.prs)
    lane = batch.prs[0].lane if batch.prs else ""
    name = "+".join(group)

    reasons: list[str] = []
    hold_until, last = served.snapshot(group)
    if hold_until > now():
        reasons.append(f"post-merge hold for {name} until {int(hold_until - now())}s")

    served_now = served.served_this_tick(group)
    if served_now and served_now != repo:
        # Whose turn it is, in the group's own terms: when the repo that just
        # deployed is also the one that went last, the turn it used came from
        # the group and the group owes it to this repo next time.
        if last == served_now:
            reasons.append(f"exclusive group {name}: {served_now} went last, {repo} has the turn")
        else:
            reasons.append(f"exclusive group {name}: {served_now} already deployed this tick")

    if last == repo and served.planned_repos(group) - {repo}:
        # The group owes a turn and a peer in this plan can take it.  This is
        # what keeps an idle peer from holding a busy repo forever: with no
        # peer here, the group takes the turn back and the queue ships.
        waiting = sorted(served.planned_repos(group) - {repo})
        reasons.append(f"exclusive group {name}: {repo} went last, {waiting[0]} has the turn")

    if not reasons:
        return None
    return BatchOutcome(batch.index, repo, "held", "; ".join(reasons), pr_numbers, lane=lane)


def _batch_key(repo: str, deploy_unit: str, prs: Sequence[ApprovedPR]) -> str:
    """A stable identity for one batch, so a retry recognises its own work."""
    numbers = ",".join(str(p.pr_number) for p in prs)
    return f"{repo}|{deploy_unit}|{numbers}"


def _retry_half_shipped(
    batch: Batch,
    *,
    spec: ExecutorSpec,
    repo_spec: RepoSpec,
    ledger: HoldLedger,
    emit: _Emitter,
    dry_run: bool,
) -> BatchOutcome | None:
    """Finish a batch whose merge landed but whose deploy did not, or ``None``.

    GitHub says a merged PR is ``MERGED`` forever, so a batch that merged and
    then failed to deploy is never eligible again.  Every later tick reported
    it ``skipped (already merged)`` with exit code 0: the work was on main,
    production never saw it, and the queue looked perfectly healthy.  A queue
    that stops moving has to say why, so the executor remembers the one fact
    GitHub does not -- that this batch's deploy is still owed -- and retries it.
    """
    repo = batch.repo
    pr_numbers = tuple(p.pr_number for p in batch.prs)
    lane = batch.prs[0].lane if batch.prs else ""
    key = _batch_key(repo, batch.deploy_unit, batch.prs)

    if dry_run:
        return None

    outstanding = ledger.pending_deploy(key)
    if not outstanding:
        return None
    merge_sha = str(outstanding.get("merge_sha") or "")
    emit("merge.deploy_retry", repo=repo, batch=batch.index, merge_sha=merge_sha)
    return _deploy_and_verify(
        batch,
        repo=repo,
        pr_numbers=pr_numbers,
        lane=lane,
        conflicting=(),
        spec=spec,
        repo_spec=repo_spec,
        emit=emit,
        dry_run=dry_run,
        merged_sha=merge_sha,
        runnable=[],
        ledger=ledger,
        pending_key=key,
    )


def _process_batch(
    batch: Batch,
    *,
    spec: ExecutorSpec,
    repo_specs: dict[str, RepoSpec],
    ledger: HoldLedger,
    served: _ServedState,
    client: PRReader,
    emit: _Emitter,
    dry_run: bool,
    now: Callable[[], float],
) -> BatchOutcome:
    """Take one batch from planned to merged/deployed/verified, or explain why not."""
    repo = batch.repo
    pr_numbers = tuple(p.pr_number for p in batch.prs)
    lane = batch.prs[0].lane if batch.prs else ""
    repo_spec = repo_specs.get(repo)

    if repo_spec is None:
        return BatchOutcome(
            batch.index, repo, "skipped", "no repo spec configured", pr_numbers, lane=lane
        )
    if not (repo_spec.merge_template or repo_spec.merge_per_pr_template):
        return BatchOutcome(
            batch.index,
            repo,
            "skipped",
            "(no merge template configured)",
            pr_numbers,
            lane=lane,
        )

    for hold in ledger.active_holds(spec):
        if any(hold.matches(lane=p.lane, deploy_unit=batch.deploy_unit) for p in batch.prs):
            emit("merge.held", repo=repo, batch=batch.index, hold=hold.name, prs=list(pr_numbers))
            return BatchOutcome(
                batch.index,
                repo,
                "held",
                f"cluster hold {hold.name} (release: fleet merge release {hold.name})",
                pr_numbers,
                lane=lane,
            )

    block = _group_block(batch, spec=spec, served=served, now=now)
    if block is not None:
        emit("merge.held", repo=repo, batch=batch.index, reason=block.detail, prs=list(pr_numbers))
        return block

    lock = deploy_lock_for(Path(spec.state_dir).expanduser(), repo)
    if dry_run:
        # A dry run must not take a lock: reporting what would happen must not
        # itself block the real process that is about to do it.
        return _run_batch(
            batch,
            spec=spec,
            repo_spec=repo_spec,
            client=client,
            emit=emit,
            dry_run=True,
            now=now,
            ledger=ledger,
            served=served,
        )
    if not lock.try_acquire():
        meta = lock.read_meta()
        holder = meta.get("pid")
        detail = f"another merge holds {repo}'s deploy lock (pid {holder})"
        emit("merge.locked", repo=repo, batch=batch.index, holder_pid=holder)
        return BatchOutcome(batch.index, repo, "locked", detail, pr_numbers, lane=lane)

    try:
        return _run_batch(
            batch,
            spec=spec,
            repo_spec=repo_spec,
            client=client,
            emit=emit,
            dry_run=dry_run,
            now=now,
            ledger=ledger,
            served=served,
        )
    finally:
        # Every exit path above -- return, exception, KeyboardInterrupt --
        # lands here, so the lock cannot outlive the process that took it.
        lock.release()


def _run_batch(
    batch: Batch,
    *,
    spec: ExecutorSpec,
    repo_spec: RepoSpec,
    client: PRReader,
    emit: _Emitter,
    dry_run: bool,
    now: Callable[[], float],
    ledger: HoldLedger,
    served: _ServedState,
) -> BatchOutcome:
    """Merge, deploy, and verify one batch. The caller holds its deploy lock."""
    repo = batch.repo
    pr_numbers = tuple(p.pr_number for p in batch.prs)
    lane = batch.prs[0].lane if batch.prs else ""
    repo_path = Path(repo_spec.path).expanduser() if repo_spec.path else None

    half_shipped = _retry_half_shipped(
        batch,
        spec=spec,
        repo_spec=repo_spec,
        ledger=ledger,
        emit=emit,
        dry_run=dry_run,
    )
    if half_shipped is not None:
        return half_shipped

    mergeable, conflicting, skipped = _partition_prs(batch.prs, client=client, repo_path=repo_path)

    rebase_commands: list[str] = []
    for conflicting_pr in conflicting:
        emit(
            "merge.needs_rebase",
            repo=repo,
            pr=conflicting_pr.pr_number,
            lane=conflicting_pr.lane,
            reason="conflicting",
        )
        rendered = _run_rebase(
            conflicting_pr, spec=spec, repo_spec=repo_spec, emit=emit, dry_run=dry_run
        )
        if rendered:
            rebase_commands.append(rendered)

    if not mergeable:
        detail = "every PR in this batch is conflicting" if conflicting else "no eligible PRs"
        if skipped:
            detail += f" ({'; '.join(f'#{p.pr_number}: {why}' for p, why in skipped)})"
        return BatchOutcome(
            batch.index,
            repo,
            "needs_rebase" if conflicting else "skipped",
            detail,
            pr_numbers,
            needs_rebase=tuple(p.pr_number for p in conflicting),
            commands=tuple(rebase_commands),
            lane=lane,
        )

    emit(
        "merge.start_batch",
        repo=repo,
        batch=batch.index,
        deploy_unit=batch.deploy_unit,
        prs=[p.pr_number for p in mergeable],
    )

    merge_commands = _merge_commands(mergeable, repo_spec, lane=lane)
    if not merge_commands:
        return BatchOutcome(
            batch.index,
            repo,
            "skipped",
            "(no merge template configured)",
            pr_numbers,
            needs_rebase=tuple(p.pr_number for p in conflicting),
            lane=lane,
        )
    runnable: list[str] = []
    for command in merge_commands:
        result = _run_command(
            shlex.split(command),
            cwd=repo_spec.path or None,
            timeout=spec.command_timeout_seconds,
            dry_run=dry_run,
        )
        runnable.append(command)
        if not result.ok:
            conflicting_rc = result.returncode == spec.conflict_exit_code
            emit(
                "merge.failed" if not conflicting_rc else "merge.needs_rebase",
                level="error" if not conflicting_rc else "warning",
                repo=repo,
                batch=batch.index,
                command=command,
                rc=result.returncode,
                timed_out=result.timed_out,
            )
            return BatchOutcome(
                batch.index,
                repo,
                "needs_rebase" if conflicting_rc else "failed",
                f"command rc={result.returncode}: {command}",
                tuple(p.pr_number for p in mergeable),
                needs_rebase=(tuple(p.pr_number for p in mergeable) if conflicting_rc else ()),
                commands=(*rebase_commands, *runnable),
                lane=lane,
            )

    merged_sha = _merged_sha(mergeable, client=client, repo_path=repo_path)
    emit(
        "merge.merged",
        repo=repo,
        batch=batch.index,
        prs=[p.pr_number for p in mergeable],
        merge_sha=merged_sha,
    )

    # The key names the batch as planned, not the subset that merged, so a
    # later tick recognises the same work even when a conflicting PR was
    # dropped from it.
    pending_key = _batch_key(repo, batch.deploy_unit, batch.prs)
    if not dry_run:
        # The merge is on main from this moment on, and GitHub will report
        # these PRs MERGED for good.  Remember that their deploy is still owed
        # *before* running it, so a deploy that dies cannot take the record of
        # the failure with it.
        ledger.record_pending_deploy(batch_key=pending_key, repo=repo, merge_sha=merged_sha)

    # The group advances when the merge lands, not when the deploy finishes:
    # a peer must not start deploying the same surface while this deploy is
    # still in flight, and the merge is the event that put the group at risk.
    group = spec.group_for(repo)
    if group:
        hold_until = now() + spec.post_merge_hold_seconds
        served.record(group=group, repo=repo, hold_until=hold_until)
        if not dry_run:
            # A dry run merges nothing, so nothing durable may move: writing the
            # ledger would hand the group a turn for a merge that never
            # happened and hold the peer for a window that protects nothing.
            # The in-memory turn still advances, because reporting what the real
            # run would do means reporting which repo it would hold back.
            ledger.record_served(group=group, repo=repo, hold_until=hold_until)

    return _deploy_and_verify(
        batch,
        repo=repo,
        pr_numbers=tuple(p.pr_number for p in mergeable),
        lane=lane,
        conflicting=conflicting,
        spec=spec,
        repo_spec=repo_spec,
        emit=emit,
        dry_run=dry_run,
        merged_sha=merged_sha,
        runnable=runnable,
        rebase_commands=rebase_commands,
        ledger=ledger,
        pending_key=pending_key,
    )


def _deploy_and_verify(
    batch: Batch,
    *,
    repo: str,
    pr_numbers: tuple[int, ...],
    lane: str,
    conflicting: Sequence[ApprovedPR],
    spec: ExecutorSpec,
    repo_spec: RepoSpec,
    emit: _Emitter,
    dry_run: bool,
    merged_sha: str,
    runnable: list[str],
    ledger: HoldLedger,
    pending_key: str,
    rebase_commands: Sequence[str] = (),
) -> BatchOutcome:
    """Run the repo's deploy then verify commands for a merged batch.

    Shared by the merge path and the retry path, because a half-shipped batch
    is finished by exactly the commands a whole batch would have run.  Returns
    a ``failed`` outcome, keeping the pending record, as soon as one of them
    fails; on success it clears the record so the batch is not retried forever.
    """
    for template, event in (
        (repo_spec.deploy_template, "merge.deployed"),
        (repo_spec.verify_template, "merge.verified"),
    ):
        if not template:
            continue
        argv = command_argv(
            template,
            merge_sha=merged_sha,
            repo=repo,
            pr_args=" ".join(f"{p.pr_number}:{p.sha9}" for p in batch.prs),
        )
        result = _run_command(
            argv,
            cwd=repo_spec.path or None,
            timeout=spec.command_timeout_seconds,
            dry_run=dry_run,
        )
        runnable.append(" ".join(argv))
        if not result.ok:
            emit(
                "merge.failed",
                level="error",
                repo=repo,
                batch=batch.index,
                command=" ".join(argv),
                rc=result.returncode,
                timed_out=result.timed_out,
            )
            return BatchOutcome(
                batch.index,
                repo,
                "failed",
                f"{event} command rc={result.returncode}",
                pr_numbers,
                needs_rebase=tuple(p.pr_number for p in conflicting),
                merged_sha=merged_sha,
                commands=(*rebase_commands, *runnable),
                lane=lane,
            )
        emit(event, repo=repo, batch=batch.index, merge_sha=merged_sha)

    if not dry_run:
        ledger.clear_pending_deploy(pending_key)
    return BatchOutcome(
        batch.index,
        repo,
        "merged",
        f"{len(batch.prs)} PR(s) merged at {merged_sha[:9]}" if merged_sha else "merged",
        pr_numbers,
        needs_rebase=tuple(p.pr_number for p in conflicting),
        merged_sha=merged_sha,
        commands=(*rebase_commands, *runnable),
        lane=lane,
    )


def _merged_sha(prs: Sequence[ApprovedPR], *, client: PRReader, repo_path: Path | None) -> str:
    """The merge commit of the last PR that merged, or ``""`` if unreadable.

    Deploy and verify templates address the merge commit, not the PR head: the
    head is what a human reviewed, the merge commit is what the server builds.
    """
    if not prs:
        return ""
    scoped = _scoped_client(client, repo_path)
    detail = scoped.pr_detail(prs[-1].pr_number)
    merge_commit = detail.get("mergeCommit") if isinstance(detail, dict) else None
    if isinstance(merge_commit, dict):
        return str(merge_commit.get("oid") or "")
    return str(detail.get("mergeCommit") or "") if isinstance(detail, dict) else ""


def run_tick(
    *,
    repo_specs: dict[str, RepoSpec] | None = None,
    spec: ExecutorSpec | None = None,
    plan: MergePlan | None = None,
    operator: str | None = None,
    status_dir: Path | None = None,
    lanes_root: Path | None = None,
    client: PRReader | None = None,
    repo_paths: list[str] | None = None,
    fleet_config_path: Path | None = None,
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    check_merges: bool = True,
    dry_run: bool = False,
    run_log: EventSink | None = None,
    run_id: str = "",
    now: Callable[[], float] = time.time,
) -> TickResult:
    """Run one executor tick and report what it did.

    Plans (unless *plan* is given), then takes every batch through merge,
    deploy, and verify.  A batch that policy or another process stops is
    reported with the reason, never silently dropped.
    """
    resolved_specs = spec if spec is not None else load_executor_spec(fleet_config_path)
    resolved_repos = repo_specs
    if resolved_repos is None:
        resolved_repos = resolve_repo_specs(
            list(repo_paths or []), fleet_config_path=fleet_config_path
        )

    emit = _Emitter(run_log)
    effective_run_id = run_id or f"merge-run-{int(now())}"
    emit(
        "merge.start",
        run_id=effective_run_id,
        repos=sorted(resolved_repos),
        dry_run=dry_run,
    )

    if plan is None:
        if not resolved_repos:
            emit("merge.end", run_id=effective_run_id, outcome="no-repos", batches=0)
            return TickResult(run_id=effective_run_id, events=tuple(emit.events))
        plan = build_plan(
            repo_specs=resolved_repos,
            operator=operator,
            status_dir=status_dir,
            lanes_root=lanes_root,
            client=client if isinstance(client, GitHubClient) else None,
            max_batch_size=max_batch_size,
            check_merges=check_merges,
        )

    gh_client: PRReader = client if client is not None else GitHubClient()

    ledger = load_ledger(resolved_specs)
    served = _ServedState(ledger)
    served.prime(resolved_specs.exclusive_groups)
    served.note_planned(resolved_specs.exclusive_groups, [b.repo for b in plan.batches])
    outcomes: list[BatchOutcome] = []
    for batch in plan.batches:
        outcomes.append(
            _process_batch(
                batch,
                spec=resolved_specs,
                repo_specs=resolved_repos,
                ledger=ledger,
                served=served,
                client=gh_client,
                emit=emit,
                dry_run=dry_run,
                now=now,
            )
        )

    result = TickResult(
        outcomes=tuple(outcomes), events=tuple(emit.events), run_id=effective_run_id
    )
    emit(
        "merge.end",
        run_id=effective_run_id,
        outcome="merged" if result.by_status("merged") else "idle",
        **{k: v for k, v in result.to_dict().items() if k != "run_id"},
    )
    return TickResult(
        outcomes=result.outcomes,
        events=tuple(emit.events),
        run_id=effective_run_id,
    )


def run_daemon(
    interval: float,
    *,
    stop: Callable[[], bool] | None = None,
    max_ticks: int | None = None,
    repo_paths: list[str] | None = None,
    fleet_config_path: Path | None = None,
    spec: ExecutorSpec | None = None,
    operator: str | None = None,
    status_dir: Path | None = None,
    dry_run: bool = False,
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    check_merges: bool = True,
) -> list[TickResult]:
    """Run a tick every *interval* seconds until asked to stop.

    *stop* and *max_ticks* are the seams the tests and the signal handler use;
    production passes a closure over a ``SIGINT``/``SIGTERM`` flag.  The daemon
    adds no behaviour of its own -- every tick is the same ``run_tick`` an
    operator would run by hand.  The parameters are spelled out rather than
    forwarded through ``**kwargs`` so a typo in one is a type error, not a
    silently ignored setting.
    """
    results: list[TickResult] = []
    while True:
        results.append(
            run_tick(
                repo_paths=repo_paths,
                fleet_config_path=fleet_config_path,
                spec=spec,
                operator=operator,
                status_dir=status_dir,
                max_batch_size=max_batch_size,
                check_merges=check_merges,
                dry_run=dry_run,
            )
        )
        if max_ticks is not None and len(results) >= max_ticks:
            return results
        if stop is not None and stop():
            return results
        time.sleep(interval)
