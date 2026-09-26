"""One agent owns a PR end to end, instead of a fresh agent per round.

The verifier/judge/fixer loop re-reads the PR from scratch every round: no
memory of which claim was already argued down, no memory of which test command
actually works in this repo, no memory of the task the lane started from. This
module keeps that context in one file, ``.agent-fleet/pr/<n>/notes.md``, and
spends a single engine call per round on top of it.

One round is: read the notes, fold the new findings and failing test ids into
them, build ONE fix prompt, run the configured engine once in the lane's
worktree, re-run the PR's tests, append the outcome back to the notes, report
``{pushed, new_head, fixed, disputed, tests}``.

The net loop is unchanged — the gate still re-judges every round — but the
engine is told what the previous rounds already established, so it disputes a
bad claim by citing the earlier round instead of re-deriving it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shlex
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_fleet.contracts.gate import Finding, FindingsReport
from agent_fleet.gate.prompts import AGENT_RULES
from agent_fleet.integrations.github_cli import gh

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agent_fleet.backends import LLMBackend
    from agent_fleet.repo import RepoConfig

logger = logging.getLogger(__name__)

__all__ = [
    "PrOwnership",
    "WorktreeBusyError",
    "build_fix_prompt",
    "load_findings",
    "own_round",
    "pr_head",
    "pr_notes_lock_path",
    "pr_notes_path",
    "read_notes",
    "read_task_spec",
    "render_round",
    "resolve_answer",
    "run_own",
    "write_notes",
]

#: How much of the prior history travels back into the prompt. The notes grow
#: every round, so an unbounded splice would eventually crowd out the findings
#: that round actually has to act on.
NOTES_HISTORY_CHARS = 6000

#: What the engine is asked to end its answer with. Read back out of stdout to
#: tell "this round fixed nothing" from "this round fixed three things", so it
#: is a fenced JSON block: the only shape a model reproduces reliably.
ANSWER_TAG = "AGENT_FLEET_PR_OWNER_ANSWER"

ANSWER_SPEC: dict[str, Any] = {
    "fixed": ["<finding number>"],
    "disputed": [{"id": "<finding number>", "why": "<why the claim does not hold>"}],
}

NOTHING_TO_DO = "There is nothing to fix this round."


# ---------------------------------------------------------------------------
# The notes file — the memory that carries between rounds
# ---------------------------------------------------------------------------


def pr_notes_path(repo_path: Path, pr_number: int) -> Path:
    """Where PR *pr_number*'s ownership notes live: ``.agent-fleet/pr/<n>/notes.md``."""
    return Path(repo_path) / ".agent-fleet" / "pr" / str(pr_number) / "notes.md"


def pr_notes_lock_path(repo_path: Path, pr_number: int) -> Path:
    """The sidecar lock that guards PR *pr_number*'s notes.

    A separate file from the notes, and it exists for one reason: ``write_notes``
    commits by ``os.replace``, so the notes' own inode is replaced on every
    write. A ``flock`` taken on that inode guards the *old* file, not the name —
    the instant the rename lands the lock is on an unlinked inode and a second
    round can walk straight in. This path is opened and flocked but never
    rewritten, so its inode is stable and the lock outlives every rename of the
    notes it guards. Same convention as the pool lock in
    :func:`agent_fleet.slots.record_size`.
    """
    return pr_notes_path(repo_path, pr_number).with_name("notes.lock")


def read_notes(repo_path: Path, pr_number: int) -> str:
    """The notes so far, or ``""`` for a PR no owner has touched."""
    path = pr_notes_path(repo_path, pr_number)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_notes(repo_path: Path, pr_number: int, text: str) -> Path:
    """Persist *text* as the PR's notes, creating the directory.

    Written to a sibling temp file and renamed into place: the notes are the
    whole multi-round memory of this PR, and an in-place rewrite truncates them
    to nothing the instant the process is killed between the open and the last
    write. A reader therefore sees either the previous notes or the new ones,
    never a half-written file.

    The temp name carries this writer's own identity, not just the pid: a pid is
    one path per *process*, so two threads of one round-append would share it,
    and the loser's ``replace`` would fail on a file the winner had already
    moved away.
    """
    path = pr_notes_path(repo_path, pr_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text if text.endswith("\n") else f"{text}\n"
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


@contextlib.contextmanager
def _flock_path(path: Path, *, create: bool) -> Iterator[int]:
    """Hold ``LOCK_EX`` on *path* for the body; yield ``-1`` if it cannot be taken.

    A path that cannot be opened degrades to running unguarded rather than
    failing the round: the notes are the only writer's own record of what it
    did, and a lock the filesystem will not give us must not cost the round its
    record entirely. The outer guard in :func:`_append` is a separate, stable
    path, so a failure here still leaves the section serialised.
    """
    handle = -1
    try:
        handle = os.open(path, os.O_RDONLY | os.O_CREAT if create else os.O_RDONLY, 0o644)
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:
        if handle >= 0:
            with contextlib.suppress(OSError):
                os.close(handle)
        yield -1
        return
    try:
        yield handle
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(handle)


def _append(repo_path: Path, pr_number: int, block: str) -> None:
    """Add one round's outcome to the notes without losing the earlier ones.

    The read and the write are one critical section: a round that reads a
    half-finished notes file, or reads the file and then has another round's
    append land before its own write, would splice a stale copy over the
    history.

    Two locks guard it, and both are needed because ``write_notes`` commits by
    ``os.replace``. The *notes-path* flock is what a competing writer of the
    same shape contends on, but the rename swaps the notes for a new inode, so
    the moment the write lands that flock guards an unlinked inode and stops
    excluding — two same-PR rounds can then enter the read-modify-write at once
    and erase each other's block. The *sidecar* lock is the one that survives
    the rename: it is never rewritten, so its inode is stable and it keeps
    serialising every writer for the whole section. The sidecar is taken first
    and the notes flock second, so the notes flock is always a strictly inner
    guard, never the outer one. See :func:`pr_notes_lock_path`.
    """
    lock_path = pr_notes_lock_path(repo_path, pr_number)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with _flock_path(pr_notes_path(repo_path, pr_number), create=True):
                base = read_notes(repo_path, pr_number) or _seed(
                    pr_number, "", "", "(none recorded)"
                )
                write_notes(repo_path, pr_number, f"{base.rstrip()}\n\n{block.rstrip()}\n")
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _seed(pr_number: int, head: str, task_spec: str, test_command: str) -> str:
    """The notes a first round starts from. Seeded from the task file, because
    the spec is the one thing no earlier round can recover on its own."""
    sections = [
        f"# PR #{pr_number} ownership notes",
        "",
        f"Head at first round: `{head}`",
        "",
        "## Task spec",
        "",
        task_spec.strip() or "(none supplied — ask before changing intent)",
        "",
        "## Test commands that work",
        "",
        f"- `{test_command}`",
        "",
        "## Rounds",
        "",
    ]
    return "\n".join(sections)


def render_round(
    *,
    head: str,
    findings: Sequence[Finding],
    fixed: Sequence[str] = (),
    disputed: Sequence[dict[str, str]] = (),
    tests_ok: bool = True,
    failing: Sequence[str] = (),
    new_head: str = "",
    timestamp: str | None = None,
) -> str:
    """The markdown for one round's outcome, ready to append to the notes.

    Bounded: a round records what changed about the *findings*, not the diff.
    A verdict that flipped is the one thing the next round must not re-litigate
    by accident, so it is written down even when the outcome is "nothing to do".
    """
    stamp = timestamp or datetime.now(UTC).isoformat(timespec="seconds")
    head_note = f" → `{new_head}`" if new_head else ""
    lines = [
        f"### Round {stamp}",
        "",
        f"- head: `{head}`{head_note}",
        f"- findings in: {len(findings)}",
        f"- fixed: {', '.join(fixed) if fixed else 'none'}",
    ]
    if disputed:
        detail = "; ".join(f"{d.get('id', '?')}: {d.get('why', '')}" for d in disputed)
        lines.append(f"- disputed: {detail}")
    else:
        lines.append("- disputed: none")
    lines.append(f"- tests: {'pass' if tests_ok else 'FAIL'}")
    if failing:
        lines.append(f"- failing test ids: {', '.join(failing)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Findings and the PR head
# ---------------------------------------------------------------------------


def load_findings(path: Path | None) -> list[Finding]:
    """Read a findings JSON file, or return ``[]`` when no file was given.

    Accepts the gate's own dialect (a ``{"findings": [...]}`` report) and a bare
    top-level list, so a caller can pipe either shape in.
    """
    if path is None:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        raw = {"findings": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a findings object or list, got {type(raw).__name__}")
    return FindingsReport.from_dict(raw).findings


def pr_head(pr_number: int, repo_path: Path) -> tuple[str, str]:
    """``(head_ref, head_oid)`` for a PR.

    One ``gh pr view`` for both fields: the branch to work on and the sha we
    must not race both come from the same snapshot, so a push landing between
    two calls cannot leave the owner working a branch that is no longer the head.
    """
    result = gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "headRefName,headRefOid",
        cwd=repo_path,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view {pr_number} failed in {repo_path}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh pr view {pr_number} returned non-JSON: {result.stdout[:200]}"
        ) from exc
    return str(payload.get("headRefName") or ""), str(payload.get("headRefOid") or "")


# ---------------------------------------------------------------------------
# The one prompt, and the one answer
# ---------------------------------------------------------------------------


def build_fix_prompt(
    *,
    pr_number: int,
    head: str,
    branch: str,
    worktree: str,
    findings: Sequence[Finding],
    failing: Sequence[str] = (),
    prior_notes: str = "",
    test_command: str = "",
) -> str:
    """Assemble the single prompt for one ownership round.

    Findings are numbered rather than listed bare, because the answer is keyed
    by the same numbers: a model that has to echo a finding's id string back
    will misspell or truncate it, and a misspelled id reads as "fixed nothing".
    """
    lines = [
        AGENT_RULES.rstrip(),
        "",
        f"PR #{pr_number}, branch `{branch}`, worktree {worktree}, head `{head}`.",
        "",
        "You are the OWNER of this PR. You have been fixing it across several "
        "rounds and the notes below are your own memory of the earlier ones. Fix "
        "everything listed here in one pass, then commit and push.",
        "",
        "## Findings to address",
        "",
    ]
    if findings:
        for index, finding in enumerate(findings, start=1):
            lines.extend(
                [
                    f"{index}. [{finding.id}] {finding.file}:{finding.line}",
                    f"   claim: {finding.claim}",
                    f"   repro: {finding.repro}",
                    "",
                ]
            )
    else:
        lines.extend([NOTHING_TO_DO, ""])

    if failing:
        lines.append("## Tests failing right now")
        lines.append("")
        lines.extend(f"- {test_id}" for test_id in failing)
        lines.append("")

    if prior_notes.strip():
        lines.append("## Your notes from the earlier rounds")
        lines.append("")
        lines.append(f"(the most recent {NOTES_HISTORY_CHARS} characters)")
        lines.append("")
        lines.append(prior_notes[-NOTES_HISTORY_CHARS:])
        lines.append("")

    lines.extend(
        [
            "## Rules",
            "",
            "- Fix the product code so every finding above is genuinely addressed. "
            "Do not weaken a test to make a finding go away.",
            "- If a finding is wrong, say so in the answer below instead of editing "
            "code to satisfy it. A disputed claim is carried into the next round, "
            "so state the reason, not just the disagreement.",
            f"- Verify with: `{test_command}` (run it from the worktree root, memory-capped).",
            "- Touch nothing unrelated. Never `git reset --hard`, `git checkout --`, "
            "`git clean`, or `git stash` — other lanes share this machine.",
            "- Commit (never `--no-verify`; skip only a named hook that is red on "
            "baseline debt outside your diff) and push to the branch.",
            "",
            "## Answer",
            "",
            f"End your message with exactly one fenced json block tagged {ANSWER_TAG}, "
            "in this shape:",
            "",
            "```json",
            json.dumps(ANSWER_SPEC, indent=2),
            "```",
            "",
            "Report the findings you actually fixed by the numbers above, in the "
            '"fixed" list — not by their ids. If you fixed none, say so — an empty '
            "round is a real answer, and reporting it honestly is what lets the next "
            "round move on.",
        ]
    )
    return "\n".join(lines)


def parse_answer(stdout: str) -> dict[str, list[Any]]:
    """Read the tagged answer block back out of engine stdout.

    Tolerates a missing or malformed block by reporting an empty round: a model
    that fixed the findings but garbled the trailer must not be read as having
    fixed nothing and sent round after round.
    """
    marker = stdout.rfind(ANSWER_TAG)
    if marker < 0:
        return {"fixed": [], "disputed": []}
    tail = stdout[marker:]
    open_fence = tail.find("```")
    close_fence = tail.rfind("```")
    if open_fence < 0 or close_fence <= open_fence:
        return {"fixed": [], "disputed": []}
    # Skip past the opening fence *line*: the ```json language marker would
    # otherwise be handed to json.loads and fail every well-formed answer.
    body_start = tail.find("\n", open_fence)
    if body_start < 0 or body_start >= close_fence:
        return {"fixed": [], "disputed": []}
    try:
        payload = json.loads(tail[body_start + 1 : close_fence])
    except json.JSONDecodeError:
        return {"fixed": [], "disputed": []}
    if not isinstance(payload, dict):
        return {"fixed": [], "disputed": []}
    fixed = [str(x) for x in cast("list[Any]", payload.get("fixed") or [])]
    raw_disputed = payload.get("disputed")
    disputed = [
        {"id": str(d.get("id", "")), "why": str(d.get("why", ""))}
        for d in cast("list[Any]", raw_disputed or [])
        if isinstance(d, dict)
    ]
    return {"fixed": fixed, "disputed": disputed}


def resolve_answer(
    answer: dict[str, list[Any]], findings: Sequence[Finding]
) -> dict[str, list[Any]]:
    """Map the numbers the engine answered with back onto finding ids.

    The prompt numbers the findings and the answer spec asks for those numbers,
    so a round fixed by number is the only round shape there is. This is the
    seam that turns a number into the id the notes and the caller record, and
    it also accepts an id echoed directly — a model that reports one anyway is
    understood rather than silently read as having fixed nothing. A token that
    is neither is dropped: an unmatched entry in the notes would be a finding
    the next round looks for and cannot find.
    """
    by_number = {str(i): f.id for i, f in enumerate(findings, start=1)}
    by_id = {f.id: f.id for f in findings}

    def _resolve(token: str) -> str | None:
        key = token.strip().lstrip("#").strip()
        if key in by_number:
            return by_number[key]
        return by_id.get(key)

    fixed = [
        resolved for token in map(str, answer.get("fixed") or []) if (resolved := _resolve(token))
    ]
    disputed = [
        {"id": resolved, "why": str(entry.get("why", ""))}
        for entry in cast("list[Any]", answer.get("disputed") or [])
        if isinstance(entry, dict) and (resolved := _resolve(str(entry.get("id", ""))))
    ]
    return {"fixed": fixed, "disputed": disputed}


# ---------------------------------------------------------------------------
# The round
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrOwnership:
    """The result of one ownership round."""

    pushed: bool
    new_head: str
    fixed: list[str]
    disputed: list[dict[str, str]]
    tests: dict[str, Any]
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pushed": self.pushed,
            "new_head": self.new_head,
            "fixed": self.fixed,
            "disputed": self.disputed,
            "tests": self.tests,
            "detail": self.detail,
        }


def _head_oid(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill the test run we started, and nothing else.

    The run is spawned with ``start_new_session=True``, so it leads its own
    process group and this reaches the pytest/xdist workers it spawned. Without
    it a timed-out run leaves orphans holding the worktree and CPU on a machine
    every other lane is sharing.
    """
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError, ProcessLookupError):
        proc.kill()


def _run_capped(
    command: str, *, cwd: Path, timeout_s: int
) -> subprocess.CompletedProcess[str] | None:
    """Run a shell *command* memory-capped, in its own process group.

    Returns ``None`` when the run timed out (its group is killed first, so
    nothing is left running) or could not be started. A timeout is an infra
    failure, not a test failure: the caller must not record failing test ids
    from a run that never got to answer.
    """
    from agent_fleet.fleet_ops.memcap import MemoryCapError, plan_memory_cap

    # Always an explicit `sh -c`: the systemd branch of the cap plan execs its
    # argv directly, so a bare command string would be execed as one word. The
    # ulimit branch adds its own shell around this one.
    try:
        plan = plan_memory_cap(["sh", "-c", command], shell=True)
    except (MemoryCapError, ValueError) as exc:
        logger.warning("no memory cap available for the test re-run: %s", exc)
        return None

    try:
        proc = subprocess.Popen(
            plan.argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        logger.warning("test re-run could not start: %s", exc)
        return None
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=5)
        return None
    return subprocess.CompletedProcess(
        plan.argv, proc.returncode if proc.returncode is not None else -1, stdout, stderr
    )


def _run_tests(worktree: Path, test_command: str, test_ids: Sequence[str]) -> dict[str, Any]:
    """Re-run the PR's tests and report which of *test_ids* still fail.

    The repo's ``test_command`` is a shell command, as every other executor of
    that field treats it (``verify_core.run_shell_verify``,
    ``command_verifier._run_shell``): it may quote arguments, chain with ``&&``,
    or set an env var. It is therefore run through a shell rather than split
    into argv, and the test ids are appended as their own quoted words.
    """
    if not test_command.strip():
        return {"ran": False, "ok": True, "failing": list(test_ids), "detail": "no test command"}
    command = " ".join([test_command, *(shlex.quote(t) for t in test_ids)])
    result = _run_capped(command, cwd=worktree, timeout_s=1800)
    if result is None:
        return {
            "ran": False,
            "ok": False,
            "failing": list(test_ids),
            "detail": "test re-run did not complete (timed out or could not start)",
        }
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    failing = _failing_from_output(combined, test_ids)
    return {
        "ran": True,
        "ok": result.returncode == 0 and not failing,
        "failing": failing,
        "exit_code": result.returncode,
        "detail": (result.stdout or result.stderr).strip()[-2000:],
    }


def _failing_from_output(stdout: str, test_ids: Sequence[str]) -> list[str]:
    """Which of the requested test ids pytest reported as failing."""
    failing = []
    for test_id in test_ids:
        if f"FAILED {test_id}" in stdout or f"ERROR {test_id}" in stdout:
            failing.append(test_id)
    return failing


def _git_capture(argv: Sequence[str], *, cwd: Path, timeout: int = 120) -> tuple[int, str]:
    """Run a git command, returning ``(returncode, output)``.

    Output is stdout and stderr together: for a rebase or a push, the part that
    says what went wrong is on stderr, and a caller reporting the failure wants
    both. A command that times out or cannot be started is a non-zero result
    with the reason as its output, never an exception, so every caller of this
    round's own git commands reaches the same reporting path.
    """
    try:
        result = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, f"git {' '.join(argv)} did not complete: {exc}"
    return result.returncode, f"{result.stdout or ''}\n{result.stderr or ''}".strip()


def _push_head(worktree: Path, branch: str, *, timeout: int = 300) -> tuple[bool, str]:
    """Push the worktree head to *branch*, rebasing once if that is not enough.

    The round's work is committed in the worktree before this runs, so every
    failure mode here has to be reported rather than raised: a hang must not
    leave the round unrecorded, and a non-fast-forward must not throw away a
    round whose fixes are already committed. A commit landing on the branch
    between the checkout and the push is ordinary — another lane, or the gate
    fix loop — and re-basing on top of it lands this round's work instead of
    discarding it.
    """
    code, out = _git_capture(["push", "origin", f"HEAD:{branch}"], cwd=worktree, timeout=timeout)
    if code == 0:
        return True, ""
    if "non-fast-forward" not in out and "fetch first" not in out:
        return False, f"push failed: {out[-500:]}"

    fetched, fetch_out = _git_capture(["fetch", "origin", branch], cwd=worktree, timeout=timeout)
    if fetched != 0:
        return (
            False,
            f"push rejected as non-fast-forward, and the refetch failed: {fetch_out[-500:]}",
        )
    rebased, rebase_out = _git_capture(
        ["rebase", f"origin/{branch}"], cwd=worktree, timeout=timeout
    )
    if rebased != 0:
        _git_capture(["rebase", "--abort"], cwd=worktree, timeout=timeout)
        return (
            False,
            f"push rejected as non-fast-forward, and the rebase failed: {rebase_out[-500:]}",
        )
    retry_code, retry_out = _git_capture(
        ["push", "origin", f"HEAD:{branch}"], cwd=worktree, timeout=timeout
    )
    if retry_code == 0:
        return True, ""
    return False, f"push failed after rebasing onto origin/{branch}: {retry_out[-500:]}"


def own_round(
    *,
    repo_path: Path,
    pr_number: int,
    findings: Sequence[Finding] = (),
    failing: Sequence[str] = (),
    task_spec: str = "",
    repo: RepoConfig | None = None,
    backend: LLMBackend | None = None,
    worktree: Path | None = None,
) -> PrOwnership:
    """Run one ownership round for *pr_number* and return what it achieved.

    *backend* and *worktree* are the injection seams: a test passes a fake
    engine and a scratch worktree and never touches the network or git. Omitted,
    the configured backend is built and the PR's own worktree is reused — which
    needs *repo*, so a caller without one gets a recorded failed round rather
    than an assertion, and the CLI can still print one line and exit 1.
    """
    repo_path = Path(repo_path)
    branch, start_oid = pr_head(pr_number, repo_path)
    if not branch:
        return PrOwnership(False, "", [], [], {"ran": False, "ok": True}, "PR has no head branch")

    if worktree is None:
        if repo is None:
            detail = "no .agent-fleet.yaml for this repo, so there is no test command to run"
            _append(
                repo_path,
                pr_number,
                render_round(head=start_oid, findings=findings, tests_ok=False, timestamp=_now()),
            )
            return PrOwnership(False, start_oid, [], [], {"ran": False, "ok": False}, detail)
        worktree = _checkout_own_worktree(branch, repo_path)

    # The baseline this round is measured against is the head *after* the
    # worktree exists, not the sha ``pr_head`` snapshotted before the fetch.
    # ``checkout_branch`` resets to ``origin/<branch>``, so a push landing in
    # that window leaves the worktree at a sha this round never authored;
    # measuring against the post-checkout head keeps that commit out of this
    # round's push and out of the notes.
    base_oid = _head_oid(worktree) or start_oid
    test_command = (repo.test_command if repo else None) or ""
    prior = read_notes(repo_path, pr_number)
    if not prior.strip():
        # First round: the seed is what every later round reads back, so it has
        # to hit the disk now, not just feed this round's prompt.
        prior = _seed(pr_number, start_oid, task_spec, test_command or "(none configured)")
        write_notes(repo_path, pr_number, prior)

    prompt = build_fix_prompt(
        pr_number=pr_number,
        head=start_oid,
        branch=branch,
        worktree=str(worktree),
        findings=findings,
        failing=failing,
        prior_notes=prior,
        test_command=test_command or "(unknown — check the repo's .agent-fleet.yaml)",
    )

    if backend is None:
        from agent_fleet.backends import make_backend
        from agent_fleet.config import load_fleet_config

        backend = make_backend(load_fleet_config())
    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=1800,
        cwd=worktree,
    )
    if result.exit_code != 0:
        detail = f"engine failed: {(result.stderr or result.stdout).strip()[-500:]}"
        _append(
            repo_path,
            pr_number,
            render_round(
                head=start_oid,
                findings=findings,
                failing=failing,
                new_head="",
                tests_ok=False,
                timestamp=_now(),
            ),
        )
        return PrOwnership(False, start_oid, [], [], {"ran": False, "ok": False}, detail)

    answer = resolve_answer(parse_answer(result.stdout), findings)
    new_head = _head_oid(worktree)
    moved = bool(new_head) and new_head != base_oid
    push_detail = ""
    pushed = moved
    if moved:
        pushed, push_detail = _push_head(worktree, branch)
        if pushed:
            # The push may have rebased this round's commit onto origin/<branch>,
            # which rewrites it to a new sha. Re-read the head from the worktree
            # so the sha reported here, written to the notes and returned in
            # to_dict() is the one actually on the branch — not the pre-rebase
            # commit, which exists nowhere and would strand the next round.
            new_head = _head_oid(worktree) or new_head

    tests = _run_tests(worktree, test_command, failing)
    # Appended before returning in every case: a round that pushed nothing, or
    # whose push failed, still happened and the next round has to know about it.
    _append(
        repo_path,
        pr_number,
        render_round(
            head=start_oid,
            findings=findings,
            fixed=answer["fixed"],
            disputed=answer["disputed"],
            tests_ok=bool(tests["ok"]),
            failing=cast("list[str]", tests.get("failing") or []),
            new_head=new_head if pushed else "",
            timestamp=_now(),
        ),
    )
    return PrOwnership(
        pushed=pushed,
        new_head=new_head,
        fixed=answer["fixed"],
        disputed=answer["disputed"],
        tests=tests,
        detail=push_detail,
    )


def _lane_worktree(repo_path: Path, branch: str) -> Path:
    from agent_fleet.pr_loop.worktree import resolve_worktree_path

    return resolve_worktree_path(
        branch,
        repo_root=repo_path,
        worktree_base=_worktree_base(repo_path),
    )


class WorktreeBusyError(RuntimeError):
    """The worktree this round would use is owned by a live process, or still
    holds a previous round's work that never reached the branch."""


def _worktree_is_git(path: Path) -> bool:
    git_dir = path / ".git"
    return git_dir.is_dir() or git_dir.is_file()


def _unpushed_work(worktree: Path) -> str:
    """Why this worktree must not be reset, or ``""`` when it is safe to reset.

    A live lock only proves that some process is in the worktree *right now*. A
    round that was killed after committing and before pushing leaves a lock
    behind whose PID is gone, so the next round reads the worktree as free and
    ``checkout_branch`` resets it onto ``origin/<branch>`` — discarding a
    commit that exists nowhere else. Uncommitted changes and commits ahead of
    the branch are what that round leaves behind, so both are checked before the
    reset, not just the lock.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode == 0 and result.stdout.strip():
        changed = len([line for line in result.stdout.splitlines() if line.strip()])
        return f"{changed} uncommitted change(s)"
    result = subprocess.run(
        ["git", "log", "--oneline", "HEAD", "--not", "--remotes"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return ""
    ahead = [line for line in result.stdout.splitlines() if line.strip()]
    if ahead:
        shas = ", ".join(line.split()[0][:9] for line in ahead[:5])
        more = f" (+{len(ahead) - 5} more)" if len(ahead) > 5 else ""
        return f"{len(ahead)} commit(s) that never reached the branch: {shas}{more}"
    return ""


def _checkout_own_worktree(branch: str, repo_path: Path) -> Path:
    """Check out *branch* for this round, refusing a worktree that still owes work.

    ``checkout_branch`` resets an existing directory to ``origin/<branch>``.
    Another lane already working this branch in that directory would lose its
    uncommitted and unpushed work, so two things are checked first: the sidecar
    lock the rest of the fleet maintains — the same guard ``remove_worktree``
    uses — and whether the directory still holds work a dead round left behind,
    which no lock can report. A reset that destroys a commit nobody else has is
    not this round's call to make, so that case raises and the round is
    reported as a failure rather than run against a silently emptied branch.
    """
    from agent_fleet.pr_loop.github_ops import checkout_branch
    from agent_fleet.pr_loop.worktree import (
        claim_worktree_lock,
        worktree_locked_by_other_process,
    )

    worktree = _lane_worktree(repo_path, branch)
    if worktree_locked_by_other_process(worktree):
        raise WorktreeBusyError(
            f"worktree {worktree} is locked by another live process; refusing to reset it"
        )
    if worktree.is_dir() and _worktree_is_git(worktree):
        stranded = _unpushed_work(worktree)
        if stranded:
            raise WorktreeBusyError(
                f"worktree {worktree} still holds {stranded}; refusing to reset it onto "
                f"origin/{branch} — recover or move that work first"
            )
    checked_out = checkout_branch(branch, worktree, repo_root=repo_path)
    claim_worktree_lock(checked_out)
    return checked_out


def read_task_spec(task_file: str | None) -> str:
    """The task spec text, or ``""`` when no file was given."""
    if not task_file:
        return ""
    return Path(task_file).read_text(encoding="utf-8")


def run_own(
    *,
    repo_path: Path,
    pr_number: int,
    findings_path: str | None = None,
    failing: Sequence[str] = (),
    task_file: str | None = None,
) -> dict[str, Any]:
    """One ownership round from raw CLI inputs.

    Reads the repo's own config and calls the configured engine; the testable
    core is :func:`own_round`, which takes both of those already resolved.
    Returns ``{"error": ...}`` rather than raising, so the CLI can print one
    message and exit 1 without catching anything. That covers the whole round,
    not just its input parsing: a bad PR number or an unauthenticated ``gh``
    raises inside :func:`pr_head`, a worktree held by another live lane raises
    out of the checkout, and a branch deleted on the remote or an auth failure
    makes the checkout's ``git fetch`` raise
    :class:`subprocess.CalledProcessError`. The git commands the round runs
    itself are handled in :func:`_push_head` rather than here, so a failed or
    timed-out push is recorded as a round outcome instead of ending the round.
    """
    import yaml

    from agent_fleet.repo import resolve_repo_config

    repo_path = Path(repo_path).expanduser().resolve()
    if not repo_path.is_dir():
        return {"error": f"repo path is not a directory: {repo_path}"}
    # Reading the repo's own config is as fallible as anything else the round
    # does: a malformed ``.agent-fleet.yaml`` raises ``yaml.YAMLError`` and an
    # ``AGENT_FLEET_TARGET_CONFIG`` pointing at a missing file raises
    # ``FileNotFoundError``. Both are caught below so the caller still gets
    # ``{"error": ...}`` instead of a traceback out of ``main``.
    try:
        repo = resolve_repo_config(repo_path)
        if repo is None:
            return {
                "error": f"no .agent-fleet.yaml for {repo_path}, so there is no test command to run"
            }
        findings = load_findings(Path(findings_path) if findings_path else None)
        task_spec = read_task_spec(task_file)
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}

    try:
        return own_round(
            repo_path=repo_path,
            pr_number=pr_number,
            findings=findings,
            failing=failing,
            task_spec=task_spec,
            repo=repo,
        ).to_dict()
    except (
        RuntimeError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        return {"error": str(exc)}


def _worktree_base(repo_path: Path) -> Path:
    from agent_fleet.repo import resolve_repo_config

    repo = resolve_repo_config(repo_path)
    base = repo.worktree_base if repo else None
    return base or (Path(repo_path) / ".worktrees" / "pr")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
