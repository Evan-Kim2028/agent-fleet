"""The PR guarantee — the failure this whole lane exists to eliminate.

``#1 recurring failure`` in the bash drivers: the implementer finished, the
lane had real work on the branch, and *no PR existed* — so the review loop, the
gate, and the automerge all had nothing to act on and the work sat forever. The
old drivers detected this and wrote ``NEEDS-ATTENTION no PR`` for a human; every
one of those lanes was a human having to go commit, push, and open a PR by hand.

This module closes that loop. After the implementer returns — successfully,
lazily, or having died — the manager guarantees the outcome:

* Uncommitted changes → committed **with hooks live**, skipping only the hook
  ids the repo config lists in ``baseline_skip_hooks``.
* Unpushed commits → pushed.
* No PR for the branch → one is opened.

Only when there is genuinely nothing to publish (a clean tree, no commits ahead
of base) does it give up, and then it says so explicitly with a reason.

On hooks: the manager uses plain ``git commit`` so the repo's real hooks run.
``--no-verify`` is never used. There are two ways a hook can be stepped around,
and the difference between them is the whole point:

* ``baseline_skip_hooks`` — those ids are skipped up front and never run. A
  blunt instrument, kept because repos already configure it.
* ``baseline_hooks`` — *verified* baseline debt. The first commit runs every
  hook live. Only a failure naming one of these earns a retry, and only after
  each one has been re-run against the lane's own changed files and passed
  there. A hook that is red about this diff is still red, still fails the lane,
  and is never skipped.

The second exists because a repo-wide hook that fails on the base branch's own
debt blocks every lane in the repo, and the operator's only way out is to skip
it for everything — which then waves through real failures too. Verifying
against the lane's own files keeps the two apart, and the ids that were bypassed
are recorded in the commit message and the PR body, because a hook that never
ran leaves no other trace.

If a non-baseline hook fails, the commit fails and the lane escalates with the
hook output attached, which is the correct outcome: that is a real problem with
the diff.
"""

from __future__ import annotations

import dis
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_fleet.fleet_ops.config import DEFAULT_FIXER_TIMEOUT_S, FIXER_PLACEHOLDER

#: A subprocess callable, injected by callers that need to stub ``gh``.
Runner = Callable[..., subprocess.CompletedProcess[str]]


logger = logging.getLogger(__name__)

#: Commit subject for the manager's own commit.
AUTO_COMMIT_SUBJECT = "fleet: auto-commit after {engine} run"

#: Never pass these to git. Asserted in ``_git`` so a refactor cannot regress it.
FORBIDDEN_GIT_FLAGS = ("--no-verify", "-n")

#: The run-dir path that must never be staged, relative to the worktree. The
#: guarantee passes it as an explicit ``git add`` pathspec exclusion rather than
#: trusting ``info/exclude`` to be present and correct — the exclude file is
#: written by the worktree step, and this function is also called directly.
RUN_DIR_EXCLUDE = ".agent-fleet"

#: The engine's transcript directory, relative to a worktree root. Staging
#: excludes this subtree rather than all of ``RUN_DIR_EXCLUDE``, so a lane's
#: real work under a *tracked* ``.agent-fleet/`` is still committed.
RUN_DIR_LOGS = f"{RUN_DIR_EXCLUDE}/runs"

#: pre-commit reports each failing hook as a ``- hook id: <id>`` block. The rest
#: of the output is hundreds of lines of tool output; the id is the only part
#: that tells an operator which hook to fix.
_HOOK_ID_RE = re.compile(r"^\s*-\s*hook id:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class GuaranteeResult:
    """Outcome of guaranteeing a PR for one lane."""

    pr: int | None
    committed: bool = False
    commit_sha: str | None = None
    pushed: bool = False
    skip_env: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    escalated: bool = False
    detail: str = ""
    #: Hook ids that refused the auto-commit, in the order pre-commit listed them.
    #: Empty for a commit failure that was not a hook failure.
    hooks_failed: list[str] = field(default_factory=list)
    #: Baseline hook ids that failed the first commit, were confirmed clean on the
    #: lane's own files, and were then bypassed with ``SKIP=``. Recorded rather
    #: than merely applied: a bypassed hook is invisible in the diff, so the only
    #: trace of it is the commit message and the PR body written from this.
    hooks_skipped: list[str] = field(default_factory=list)

    @property
    def guaranteed(self) -> bool:
        """True when a PR exists for the lane — the thing we promised."""
        return self.pr is not None and not self.escalated

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "committed": self.committed,
            "commit_sha": self.commit_sha,
            "pushed": self.pushed,
            "skip_env": self.skip_env,
            "reason": self.reason,
            "escalated": self.escalated,
            "detail": self.detail,
            "hooks_failed": self.hooks_failed,
            "hooks_skipped": self.hooks_skipped,
        }


def failed_hook_ids(output: str) -> list[str]:
    """The pre-commit hook ids named in *output*, in order, without duplicates.

    A commit can fail for reasons that are not hooks at all (a rejected
    ``user.email``, a lock file held by another process). Those produce no ids,
    which is the correct answer rather than an empty stand-in: ``hooks_failed``
    being non-empty *is* the claim that a hook refused the commit.
    """
    seen: dict[str, None] = {}
    for match in _HOOK_ID_RE.finditer(output or ""):
        seen.setdefault(match.group(1), None)
    return list(seen)


def _git(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    runner: Runner | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, refusing the forbidden hook-disabling flags.

    The guard is here rather than at each call site because ``--no-verify`` is
    exactly the kind of flag that gets added casually during a retry.
    """
    for flag in FORBIDDEN_GIT_FLAGS:
        if flag in args:
            raise ValueError(f"refusing to pass {flag!r} to git: hooks must stay enabled")
    run = runner or subprocess.run
    return run(
        list(args), cwd=cwd, capture_output=True, text=True, check=False, env=env, timeout=timeout
    )


def is_dirty(worktree: Path, *, runner: Runner | None = None) -> bool:
    """True when the worktree has staged, unstaged, or untracked *work*.

    ``git status --porcelain`` honours the repo's ``info/exclude``, so the run
    dir is invisible here once :func:`ensure_run_dir_excluded` has run. That
    matters beyond tidiness: a worktree whose only untracked file was the lane's
    own run log read as dirty, so the guarantee staged the log, committed it,
    and then failed the repo's hooks — a ``commit_failed`` for a lane that had
    produced no work at all.
    """
    result = _git(
        ["git", "status", "--porcelain", "-uall"], cwd=worktree, runner=runner, timeout=60
    )
    return bool(result.stdout.strip())


def head_sha(worktree: Path, *, runner: Runner | None = None, short: int = 0) -> str | None:
    """The worktree's HEAD sha, or None. *short* truncates to N characters.

    Truncation happens here rather than via ``git rev-parse --short N``: that
    flag takes no argument (passing one makes git exit non-zero), and the status
    line's ``sha9`` only has to match the first N characters of the full sha for
    the automerge's prefix comparison.
    """
    result = _git(["git", "rev-parse", "HEAD"], cwd=worktree, runner=runner, timeout=60)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    if not sha:
        return None
    return sha[:short] if short and short > 0 else sha


def build_commit_message(
    engine: str,
    *,
    task_file: str | None = None,
    lane: str | None = None,
    hooks_skipped: Sequence[str] = (),
) -> str:
    """Commit message for the manager's auto-commit.

    Subject is the fixed ``fleet: auto-commit after <engine> run`` convention.
    The body records provenance, so a reviewer seeing this commit in the PR
    history knows the agent did not make it and where the work came from.

    *hooks_skipped* is written into the body when the commit had to bypass any
    baseline hook. That line is the only durable record of the bypass: the hook
    leaves no trace in a diff it never saw, so a commit that passed the way this
    one did is otherwise indistinguishable from a commit that needed nothing.
    """
    subject = AUTO_COMMIT_SUBJECT.format(engine=engine)
    lines = [subject, ""]
    lines.append("Committed by the agent-fleet lane manager: the implementer left these")
    lines.append("changes in the worktree without committing them.")
    if lane:
        lines.append(f"Lane: {lane}")
    if task_file:
        lines.append(f"Task file: {task_file}")
    if hooks_skipped:
        ids = ", ".join(hooks_skipped)
        lines.append("")
        lines.append(f"Skipped baseline hooks (clean on this lane's changed files): {ids}")
    return "\n".join(lines)


def changed_files(
    worktree: Path,
    *,
    scratch_excludes: Sequence[str] = (),
    env: dict[str, str] | None = None,
    runner: Runner | None = None,
) -> list[str]:
    """The worktree-relative paths this lane changed, scratch removed.

    ``git status --porcelain -z`` rather than the newline form: a path
    containing a newline would otherwise be split into two entries, and a fixer
    would then be handed half a filename. ``-z`` NUL-separates and quotes
    nothing, so the split is exact — but a rename arrives as *two* NUL-separated
    entries, so the records are walked in order rather than iterated blindly.

    Staged, unstaged and untracked all count — a file the agent created is as
    much the lane's work as one it edited — and scratch is dropped, because a
    fixer rewriting a transcript is pure waste and a formatter touching the
    agent's own config can break the session that produced the work.

    *env* is the caller's ``SKIP=`` overlay. This read is made in the middle of
    a commit, and a git invocation that runs without the overlay the commit is
    being conducted under is a way for that commit to behave differently than the
    one the caller asked for — so the overlay travels with the call rather than
    being re-derived at each site.
    """
    result = _git(
        ["git", "status", "--porcelain", "-z", "-uall"],
        cwd=worktree,
        runner=runner,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        return []
    files: list[str] = []
    entries = (result.stdout or "").split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        files.append(path)
        # In -z mode a rename or copy is emitted as *two* consecutive NUL-
        # separated entries: "XY <new>" followed by the source path with no
        # status columns. Reading the second as its own record is what truncated
        # it into a path that does not exist ("pkg/module_one.py" -> "st_module.py"),
        # so consume it here instead. Both names are recorded: the destination is
        # what the commit touches, and the source is what a fixer and a
        # ``pre-commit run --files`` verification must also see.
        if "R" in status or "C" in status:
            source = entries[index] if index < len(entries) else ""
            if source:
                files.append(source)
                index += 1
    return [f for f in files if f and not _is_scratch(f, scratch_excludes)]


def _is_scratch(path: str, scratch_excludes: Sequence[str]) -> bool:
    """Whether *path* falls under any configured scratch prefix."""
    return any(path == p or path.startswith(p) for p in scratch_excludes if p)


def run_fixers(
    worktree: Path,
    files: Sequence[str],
    *,
    fixers: Sequence[str] = (),
    timeout: int = DEFAULT_FIXER_TIMEOUT_S,
    runner: Runner | None = None,
) -> list[str]:
    """Run the repo's fixers over *files*; return the fixers that failed.

    Each command is a shell string from repo config with ``{py}`` standing in
    for the file. A command carrying no placeholder is a whole-tree fixer by
    design (a codemod that is not a per-file one) and runs once; one that has the
    placeholder runs once per file, so a fixer that cannot parse a particular
    path does not stop the others.

    The substituted path is **shell-quoted** (see :func:`_expand_fixer`), so the
    file name is an argument and never a piece of the command. It comes from
    ``git status`` and the agent chooses it, so a name like
    ``feature.py$(id).py`` would otherwise be executed by the shell, and even an
    innocent ``report (v2).md`` would break the fixer.

    A fixer failing is recorded, never raised. The commit that follows is the
    real authority on whether these files are acceptable and will name whatever
    the fixer could not fix; turning a fixer's exit code into a lane escalation
    would fail a lane whose files are fine, on a tool's opinion of itself.
    """
    failed: list[str] = []
    run = runner or subprocess.run
    for fixer in fixers:
        command = (fixer or "").strip()
        if not command:
            continue
        # No placeholder means "this fixer takes the worktree", not "this fixer
        # takes the literal string {py}"; run it exactly once.
        targets: list[str] = [""]
        if FIXER_PLACEHOLDER in command:
            targets = list(files) or [""]
        for target in targets:
            argv = _expand_fixer(command, target)
            try:
                result = run(
                    argv,
                    shell=True,
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("fixer %r failed on %s: %s", command, target or ".", exc)
                failed.append(command)
                break
            if result.returncode != 0:
                logger.warning(
                    "fixer %r exited %d on %s: %s",
                    command,
                    result.returncode,
                    target or ".",
                    (result.stderr or result.stdout or "").strip()[:200],
                )
                failed.append(command)
                break
    return failed


def _expand_fixer(command: str, target: str) -> str:
    """*command* with ``{py}`` replaced by *target*, as one shell word.

    The substitution is quoted, and that is the whole point: the fixer string is
    trusted config, but the path it is filled with is not — it is whatever the
    agent happened to create, and it is handed to ``/bin/sh`` through
    ``shell=True``. Left bare, ``notes.py; rm -rf .`` is two commands and
    ``report (v2).md`` is a syntax error, so one agent-controlled file name
    could run arbitrary commands as the operator or stop the repo's own fixer
    from ever reaching the lane's real files.

    ``shlex.quote`` is the shell's own word-safety answer and is what the path
    needs regardless of the shell: quoting is a property of the *word*, not of
    how the command was spelled. An empty *target* (the whole-tree fixer case)
    is left alone — there is nothing to substitute and the command is the
    author's own text.
    """
    if not target:
        return command
    return command.replace(FIXER_PLACEHOLDER, shlex.quote(target))


def verify_hook_on_files(
    worktree: Path,
    hook_ids: Sequence[str],
    files: Sequence[str],
    *,
    runner: Runner | None = None,
) -> tuple[list[str], list[str]]:
    """Re-run named pre-commit hooks over *files* only: ``(clean, still_red)``.

    This is the check that makes a baseline skip safe. A repo-wide hook fails on
    the base branch's own debt; the question before bypassing one is whether it
    would *also* have failed on this lane's files. ``pre-commit run <id> --files
    ...`` answers exactly that, because the hooks framework applies each hook's
    own ``files``/``types``/``exclude`` filters to the paths it is given.

    ``--all-files`` is deliberately never used. It would re-run the hook over
    the whole tree and reproduce the baseline debt this is measuring around.

    A hook that is still red on the lane's own files is returned in *still_red*
    and is never skipped: that is a real problem with the diff, which is the one
    thing a baseline allowance must not paper over. A hook id the repo has no
    config for is also *still_red* — there is nothing to verify it against, and
    an unverifiable hook is not a clean one.
    """
    targets = [f for f in files if f]
    if not targets:
        return list(hook_ids), []
    clean: list[str] = []
    still_red: list[str] = []
    for hook_id in hook_ids:
        result = _git(
            ["pre-commit", "run", hook_id, "--files", *targets],
            cwd=worktree,
            runner=runner,
            timeout=DEFAULT_FIXER_TIMEOUT_S,
        )
        if result.returncode == 0:
            clean.append(hook_id)
        else:
            logger.info(
                "baseline hook %s is also red on the lane's own files; not skipping it",
                hook_id,
            )
            still_red.append(hook_id)
    return clean, still_red


def _stage_lane_work(
    worktree: Path,
    *,
    scratch_excludes: Sequence[str] = (),
    env: dict[str, str] | None = None,
    runner: Runner | None = None,
) -> tuple[bool, str]:
    """Stage the lane's work, leaving scratch and the engine's run logs out of the index.

    Staging is all-then-unstage rather than an exclusion pathspec, because a
    single ``git add -A`` cannot express "all of it except the run dir". Given
    a ``:(exclude)`` pathspec git still walks the ignored paths, and as soon as
    an ignored ``.agent-fleet`` exists on disk it exits 1 with "The following
    paths are ignored", aborting the whole add:

    * the stale in-worktree run dir left by a build older than the one that
      moved the run dir outside the worktree — ``ensure_lane_worktree`` only
      hides it in ``info/exclude``, it never removes it, so every reused
      pre-existing lane worktree trips this on its next run; and
    * a *tracked* ``.agent-fleet/``, where excluding the path additionally
      drops the lane's real changes to that directory.

    Either way the lane escalates as ``commit_failed`` for a worktree full of
    real work, which is the exact misreport this guarantee exists to prevent.
    So the add is unconditional — it cannot fail on an ignore rule — and the
    scratch paths are then taken back out of the index. Unstaging also covers
    a *tracked* transcript, which an exclusion pathspec could not, and
    ``info/exclude`` still governs the untracked half: a run log that no repo
    tracks never reaches the index at all.

    *scratch_excludes* is the repo-configured list of paths that are never
    the lane's work — the agent's own ``.commandcode/`` and the ``%h/`` home a
    shell driver leaves behind. They get the same unstage treatment as the run
    logs, and for the same reason: unstaging leaves them in the worktree, so
    nothing is lost, and they stop being able to *cause* a commit failure.
    """
    add = _git(
        ["git", "add", "-A", "--", "."],
        cwd=worktree,
        runner=runner,
        env=env,
        timeout=300,
    )
    if add.returncode != 0:
        return False, f"git add failed: {(add.stderr or add.stdout).strip()[:500]}"

    # `git reset` (not `rm --cached`) so an *edit* to a tracked scratch file is
    # unstaged back to HEAD rather than turned into a deletion. A path that is
    # not in the index is a no-op here, so this never fails on a clean lane.
    for path in (RUN_DIR_LOGS, *(p.strip("/ ") for p in scratch_excludes if p.strip())):
        unstage = _git(
            ["git", "reset", "-q", "--", path],
            cwd=worktree,
            runner=runner,
            env=env,
            timeout=300,
        )
        if unstage.returncode != 0:
            return (
                False,
                f"git reset failed for {path}: {(unstage.stderr or unstage.stdout).strip()[:500]}",
            )
    return True, ""


def _commit_once(
    worktree: Path,
    *,
    message: str,
    env: dict[str, str] | None,
    runner: Runner | None,
) -> tuple[bool, str, list[str]]:
    """One plain ``git commit``; returns ``(ok, detail, hooks_failed)``."""
    commit = _git(
        ["git", "commit", "-m", message],
        cwd=worktree,
        runner=runner,
        env=env,
        timeout=900,
    )
    if commit.returncode == 0:
        return True, "", []
    detail = "\n".join(p for p in (commit.stdout, commit.stderr) if p).strip()[:2000]
    return False, detail or "git commit failed", failed_hook_ids(detail)


class CommitResult:
    """The result of :func:`commit_worktree`, unpackable as a 4- or 5-tuple.

    The fifth value (``hooks_skipped``) is what the PR body and the commit
    message are built from, so it has to reach the caller; the pre-existing
    four-value shape is what every older call site in this repo unpacks. Both
    shapes are live, so rather than break one to serve the other, the result
    yields whichever one the calling frame asked for.

    It is deliberately *not* a tuple subclass: CPython's ``UNPACK_SEQUENCE``
    drains a tuple-shaped object through its own fast path and would never
    consult :meth:`__iter__`, so the arity could not be honoured. A plain
    sequence is iterated, and an exhausted iterator simply ends the unpack.

    When the arity cannot be determined the full five-value form is used, which
    is the shape the only in-tree caller (:func:`ensure_pull_request`) expects.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Sequence[Any]) -> None:
        self._values = tuple(values)

    def __iter__(self) -> Iterator[Any]:
        arity = _caller_unpack_arity()
        if arity is None or arity >= len(self._values):
            return iter(self._values)
        return iter(self._values[:arity])

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, index: int) -> object:
        return self._values[index]

    def __repr__(self) -> str:
        return f"CommitResult({self._values!r})"


def _caller_unpack_arity() -> int | None:
    """How many values the calling frame is unpacking this result into.

    CPython evaluates the right-hand side of an unpacking assignment, and only
    then executes ``UNPACK_SEQUENCE`` on it. So the frame is standing *on* that
    instruction when iteration begins, and ``f_lasti`` is its offset — not past
    it, and not the trailing ``STORE_FAST`` of some earlier statement. That is
    what makes the lookup exact: a frame that unpacks something else first, in an
    earlier statement, has a *different* ``f_lasti`` and is never confused for
    this one. Matching the most recent unpack at or before the pointer, as an
    earlier version of this did, picked up that unrelated one instead and handed
    back its arity.

    Returns None for anything unexpected (introspection unavailable, an
    instruction the interpreter is not actually running, a code object that
    cannot be disassembled) so the caller falls back to the full five-value form.
    """
    try:
        # From this helper: 0=helper, 1=__iter__, 2=the frame doing the unpack.
        frame = sys._getframe(2)
        if frame is None:
            return None
        for instruction in dis.get_instructions(frame.f_code):
            if instruction.offset != frame.f_lasti:
                continue
            if instruction.opname == "UNPACK_SEQUENCE":
                return int(instruction.arg or 0)
            return None
    except Exception:  # pragma: no cover - never let introspection break a commit
        return None
    return None


def commit_worktree(
    worktree: Path,
    *,
    engine: str,
    task_file: str | None = None,
    lane: str | None = None,
    skip_hooks: Sequence[str] = (),
    baseline_hooks: Sequence[str] = (),
    fixers: Sequence[str] = (),
    scratch_excludes: Sequence[str] = (),
    runner: Runner | None = None,
) -> CommitResult:
    """Fix, stage and commit the lane's work. Returns
    ``(committed, sha, detail, hooks_failed, hooks_skipped)`` as a
    :class:`CommitResult`, which also unpacks as the older four-value shape for
    call sites that predate ``hooks_skipped``.

    *skip_hooks* is the pre-existing up-front ``SKIP=`` overlay and keeps its
    old meaning: those ids never run. *baseline_hooks* is the stronger,
    verified form and is the one a repo should prefer.

    With *baseline_hooks* the order is the point, and it is *live first*. The
    first commit runs every hook. Only if that commit fails, and only on hooks
    the repo declared baseline, is each one re-run against the lane's own
    changed files; the ones that are clean there — red on the base branch's
    debt alone — are bypassed on a second commit, and the rest still fail the
    lane. That is what separates a repo-wide hook that has been red since
    Tuesday from a hook that is red about *this diff*: the first is debt the
    lane did not inherit, the second is the lane's problem, and a manager that
    cannot tell them apart either blocks every lane in the repo or waves through
    real failures.

    A commit can therefore succeed two ways and the caller can tell which: with
    every hook satisfied (*hooks_skipped* empty), or with baseline debt bypassed
    after it was proven not to apply to this diff (*hooks_skipped* names it).
    Only the second is reported anywhere else — the commit message and the PR
    body — because a bypassed hook is otherwise invisible to every reviewer.

    *fixers* run over the changed files before anything is staged, so a commit
    the repo's own style hooks would reject is never attempted.
    *scratch_excludes* keep the agent's own directories out of the index.
    Neither can make a commit succeed that should fail — they only keep
    avoidable failures off the path.

    ``--no-verify`` is never used.
    """
    # Overlay SKIP on the real environment: a bare {"SKIP": ...} env strips
    # PATH/HOME and breaks the very hooks the manager promises to keep live.
    # Built before the first git call so that *every* git this function makes —
    # the status read, the add, the unstaging and the commit itself — carries
    # the overlay, not just the commit.
    legacy_skip = [h.strip() for h in skip_hooks if h.strip()]
    env = {**os.environ, "SKIP": ",".join(legacy_skip)} if legacy_skip else None

    files = changed_files(worktree, env=env, scratch_excludes=scratch_excludes, runner=runner)
    if fixers and files:
        run_fixers(worktree, files, fixers=fixers, runner=runner)

    staged, stage_detail = _stage_lane_work(
        worktree, scratch_excludes=scratch_excludes, env=env, runner=runner
    )
    if not staged:
        return CommitResult((False, None, stage_detail, [], []))

    message = build_commit_message(engine, task_file=task_file, lane=lane)
    ok, detail, hooks_failed = _commit_once(worktree, message=message, env=env, runner=runner)
    if ok:
        return CommitResult((True, head_sha(worktree, runner=runner), "", [], []))

    baseline = {h.strip() for h in baseline_hooks if h.strip()}
    if not baseline or not hooks_failed:
        return CommitResult((False, None, detail, hooks_failed, []))

    # Only a failure that is *entirely* baseline hooks earns a retry. One real
    # hook in the list means this commit is not publishable, and a SKIP that
    # left that hook out would be a quiet way to ship past it.
    offending = [h for h in hooks_failed if h not in baseline]
    if offending:
        return CommitResult((False, None, detail, hooks_failed, []))

    # Re-read the index, not the worktree: a fixer can have rewritten files
    # after `changed_files` was read, and a stale list would verify a hook
    # against paths this commit no longer contains. A plain `git status`, and it
    # deliberately carries no SKIP: the verification below is the one place a
    # hook must be asked to run for real.
    staged_paths = changed_files(worktree, scratch_excludes=scratch_excludes, runner=runner)
    clean, still_red = verify_hook_on_files(
        worktree, [h for h in hooks_failed if h in baseline], staged_paths, runner=runner
    )
    if still_red or not clean:
        return CommitResult((False, None, detail, hooks_failed, []))

    # Only the hooks this commit actually *proved* are reported. The up-front
    # skip ids are not among them: `FleetOpsConfig.baseline_hook_ids()` unions
    # both spellings, so a repo that set `baseline_skip_hooks` has ids that were
    # silenced before the first commit and never ran against anything. They are
    # still *bypassed* — the retry's SKIP below has to keep carrying them, or the
    # retry is not the commit the caller asked for — but publishing them under
    # the wording build_commit_message and _default_pr_body use ("clean on this
    # lane's changed files") would claim a verification that never happened, in
    # the one durable record a bypassed hook leaves behind.
    skipped = sorted(set(clean))
    retry_env = {**os.environ, "SKIP": ",".join(sorted({*clean, *legacy_skip}))}
    # The index is empty when a hook aborts a commit, so restage: the retry has
    # to carry the same content the first attempt was refused for.
    restaged, restage_detail = _stage_lane_work(
        worktree, scratch_excludes=scratch_excludes, env=retry_env, runner=runner
    )
    if not restaged:
        return CommitResult((False, None, restage_detail, hooks_failed, []))

    message = build_commit_message(engine, task_file=task_file, lane=lane, hooks_skipped=skipped)
    ok, detail, still_failing = _commit_once(
        worktree, message=message, env=retry_env, runner=runner
    )
    if not ok:
        return CommitResult((False, None, detail, still_failing or hooks_failed, []))

    return CommitResult((True, head_sha(worktree, runner=runner), "", [], skipped))


def resolve_push_target(
    branch: str,
    *,
    cwd: Path,
    configured: str | None = None,
    runner: Runner | None = None,
) -> tuple[str, str]:
    """Decide which branch to push to.

    An *existing PR* wins over the configured push target: if the lane already
    has an open PR whose head is some other branch, pushing to the configured
    branch would create a second, competing PR and leave the real one stale.
    Returns ``(branch, why)``.
    """
    head_ref = pr_head_ref(branch, cwd=cwd, runner=runner)
    if head_ref and head_ref != configured:
        return head_ref, f"existing PR head {head_ref!r} overrides configured push target"
    if head_ref:
        return head_ref, "existing PR head matches push target"
    return configured or branch, "configured push target"


def pr_head_ref(branch: str, *, cwd: Path, runner: Runner | None = None) -> str | None:
    """The ``headRefName`` of the open PR for *branch*, if any.

    Note this looks up *by branch*; it answers "is the PR I am about to create /
    update already open, and under what head name".
    """
    run = runner or subprocess.run

    def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
        return run(args, cwd=cwd, capture_output=True, text=True, check=False, timeout=120)

    result = _gh(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,headRefName",
            "--limit",
            "1",
        ]
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not items or not isinstance(items, list):
        return None
    head = items[0].get("headRefName")
    return str(head) if head else None


def _run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: int = 180,
    runner: Runner | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *args* through the injected runner, or real ``subprocess``.

    The publish helpers in ``code_review.publish`` build their own argv but accept
    no runner, so the guarantee wraps them here instead of patching their module
    globals. That keeps the injection explicit at the one call site that needs
    it, and leaves the shared helpers untouched.
    """
    run = runner or subprocess.run
    return run(list(args), cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)


def _commits_ahead(worktree: Path, branch: str, base: str, runner: Runner | None = None) -> int:
    """Commits on *branch* ahead of *base*, tolerating missing remote refs.

    Mirrors ``publish.commits_ahead_of_base``'s fallback chain (origin/branch,
    branch, HEAD) so the same probe works before and after a push.
    """
    for tip in (f"origin/{branch}", branch, "HEAD"):
        for upstream in (f"origin/{base}", base):
            result = _run(
                ["git", "rev-list", "--count", f"{upstream}..{tip}"],
                cwd=worktree,
                timeout=30,
                runner=runner,
            )
            if result.returncode != 0:
                continue
            try:
                count = int((result.stdout or "").strip())
            except ValueError:
                continue
            if count > 0:
                return count
    return 0


def _with_hooks(detail: str, hooks_failed: Sequence[str]) -> str:
    """Prefix a hook failure's output with the ids that caused it.

    The transcript is long and truncated at an arbitrary offset, so the ids are
    repeated on the first line where a reader (or a status line) will see them.
    """
    if not hooks_failed:
        return detail
    ids = ", ".join(hooks_failed)
    return f"hooks_failed=[{ids}]\n{detail}"


def _find_pr(branch: str, *, cwd: Path, runner: Runner | None = None) -> int | None:
    result = _run(
        ["gh", "pr", "list", "--head", branch, "--json", "number", "--limit", "1"],
        cwd=cwd,
        timeout=120,
        runner=runner,
    )
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None
    try:
        items = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not items or not isinstance(items, list):
        return None
    return int(items[0]["number"])


def _push_if_ahead(worktree: Path, branch: str, runner: Runner | None = None) -> bool:
    ahead = _commits_ahead(worktree, branch, branch, runner=runner)
    if ahead <= 0:
        # Already published (or nothing to publish): do not push.
        probe = _run(
            ["git", "rev-list", "--count", f"origin/{branch}..HEAD"],
            cwd=worktree,
            timeout=30,
            runner=runner,
        )
        if probe.returncode == 0 and (probe.stdout or "").strip() == "0":
            return False
    push = _run(
        ["git", "push", "-u", "origin", f"HEAD:{branch}"],
        cwd=worktree,
        timeout=180,
        runner=runner,
    )
    if push.returncode != 0:
        logger.warning("push failed for %s: %s", branch, (push.stderr or "")[:300])
        return False
    return True


def _create_pr(
    *,
    branch: str,
    base: str,
    title: str,
    body: str,
    cwd: Path,
    runner: Runner | None = None,
) -> int | None:
    result = _run(
        [
            "gh",
            "pr",
            "create",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=cwd,
        timeout=180,
        runner=runner,
    )
    if result.returncode != 0:
        logger.warning("gh pr create failed: %s", (result.stderr or "")[:500])
        return _find_pr(branch, cwd=cwd, runner=runner)
    match = re.search(r"/pull/(\d+)", (result.stdout or "") + (result.stderr or ""))
    if match:
        return int(match.group(1))
    return _find_pr(branch, cwd=cwd, runner=runner)


def has_publishable_work(
    worktree: Path, branch: str, base: str, runner: Runner | None = None
) -> bool:
    """Whether *branch* has anything a PR could carry.

    True when the worktree holds uncommitted work *or* the branch is ahead of
    *base*. The caller needs this *before* the guarantee commits anything, because
    it is the precondition for deciding that a lane produced no work — a
    judgement about the implementer, not about git plumbing.
    """
    if is_dirty(worktree, runner=runner):
        return True
    return _commits_ahead(worktree, branch, base, runner=runner) > 0


def ensure_pull_request(
    worktree: Path,
    *,
    branch: str,
    base: str,
    engine: str,
    task_file: str | None = None,
    lane: str | None = None,
    title: str | None = None,
    body: str | None = None,
    skip_hooks: Sequence[str] = (),
    baseline_hooks: Sequence[str] = (),
    fixers: Sequence[str] = (),
    scratch_excludes: Sequence[str] = (),
    runner: Runner | None = None,
    skip_env: dict[str, str] | None = None,
    no_changes_detail: str = "",
) -> GuaranteeResult:
    """Guarantee that *branch* has an open PR. See the module docstring.

    The sequence is deliberately ordered cheapest-and-safest first: commit only
    if there is something to commit, push only if ahead, and only then look for
    a PR to create. It is idempotent — running it twice on a lane that is
    already published is a no-op that returns the same PR number.

    *no_changes_detail* is the implementer's own account of why it stopped,
    supplied by the caller which can see the engine's final text. When the lane
    has nothing to publish, that text — not a boilerplate sentence — is what
    makes the escalation actionable.
    """
    skip_env = dict(skip_env or {})
    if skip_hooks and "SKIP" not in skip_env:
        skip_env = {"SKIP": ",".join(h for h in skip_hooks if h), **skip_env}

    if not Path(worktree).is_dir():
        return GuaranteeResult(
            pr=None, reason="worktree_missing", escalated=True, detail=f"no worktree at {worktree}"
        )

    committed = False
    commit_sha: str | None = None
    hooks_skipped: list[str] = []
    if is_dirty(worktree, runner=runner):
        ok, sha, detail, hooks_failed, hooks_skipped = commit_worktree(
            worktree,
            engine=engine,
            task_file=task_file,
            lane=lane,
            skip_hooks=tuple(skip_env.get("SKIP", "").split(",")) if skip_env.get("SKIP") else (),
            baseline_hooks=baseline_hooks,
            fixers=fixers,
            scratch_excludes=scratch_excludes,
            runner=runner,
        )
        if not ok:
            # A hook refused the commit. This is a real failure with the diff,
            # not something to bypass — escalate with the hook output, and name
            # the hooks so the operator does not have to find them in it.
            return GuaranteeResult(
                pr=None,
                reason="commit_failed",
                skip_env=skip_env,
                escalated=True,
                detail=_with_hooks(detail, hooks_failed),
                hooks_failed=hooks_failed,
            )
        committed = True
        commit_sha = sha

    pushed = _push_if_ahead(worktree, branch, runner=runner)

    existing = _find_pr(branch, cwd=worktree, runner=runner)
    if existing:
        return GuaranteeResult(
            pr=existing,
            committed=committed,
            commit_sha=commit_sha,
            pushed=pushed,
            skip_env=skip_env,
            hooks_skipped=hooks_skipped,
            reason="existing PR",
        )

    if _commits_ahead(worktree, branch, base, runner=runner) <= 0:
        summary = (
            f"{branch} has no commits ahead of {base} and the worktree is clean — "
            "the implementer produced no publishable work"
        )
        return GuaranteeResult(
            pr=None,
            committed=committed,
            commit_sha=commit_sha,
            pushed=pushed,
            skip_env=skip_env,
            hooks_skipped=hooks_skipped,
            reason="no_changes_stopped" if no_changes_detail else "no_commits_ahead",
            escalated=True,
            detail=f"{summary}\n{no_changes_detail}".strip() if no_changes_detail else summary,
        )

    pr = _create_pr(
        branch=branch,
        base=base,
        title=title or _default_pr_title(lane=lane, engine=engine, task_file=task_file),
        body=body
        or _default_pr_body(
            lane=lane,
            engine=engine,
            task_file=task_file,
            hooks_skipped=hooks_skipped,
        ),
        cwd=worktree,
        runner=runner,
    )
    if pr is None:
        return GuaranteeResult(
            pr=None,
            committed=committed,
            commit_sha=commit_sha,
            pushed=pushed,
            skip_env=skip_env,
            hooks_skipped=hooks_skipped,
            reason="pr_create_failed",
            escalated=True,
            detail=f"gh pr create did not yield a PR for {branch}",
        )

    return GuaranteeResult(
        pr=pr,
        committed=committed,
        commit_sha=commit_sha,
        pushed=pushed,
        skip_env=skip_env,
        hooks_skipped=hooks_skipped,
        reason="pr_created",
    )


def _default_pr_title(*, lane: str | None, engine: str, task_file: str | None) -> str:
    """PR title when the caller did not supply one.

    Prefers the task file's first heading — that is the real description of the
    work — and falls back to a lane/engine label when there is no task file.
    """
    if task_file:
        heading = _first_heading(Path(task_file))
        if heading:
            return heading[:120]
    if lane:
        return f"[fleet/{lane}] {lane} ({engine})"
    return f"fleet lane ({engine})"


def _first_heading(path: Path) -> str | None:
    """First markdown H1 in *path*, or None. Never raises."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
    return None


def _default_pr_body(
    *,
    lane: str | None,
    engine: str,
    task_file: str | None,
    hooks_skipped: Sequence[str] = (),
) -> str:
    lines = [
        "Opened automatically by the agent-fleet lane manager.",
        "",
        f"**Engine:** `{engine}`",
    ]
    if lane:
        lines.append(f"**Lane:** `{lane}`")
    if task_file:
        lines.append(f"**Task file:** `{task_file}`")
    if hooks_skipped:
        # A reviewer looking at a diff cannot tell which hooks passed it and
        # which were bypassed, so the bypass is stated here rather than left
        # implicit. Each id was re-run against this lane's changed files and
        # passed there; what it fails on is the base branch's own debt.
        ids = ", ".join(f"`{h}`" for h in hooks_skipped)
        lines.append(f"**Baseline hooks skipped on commit:** {ids}")
        lines.append("")
        lines.append(
            "These hooks failed the first commit attempt on pre-existing debt outside "
            "this diff. Each was re-run against the files this PR changes and passed "
            "there, so the diff itself is clean by their own standard."
        )
    lines.extend(
        [
            "",
            "The implementer did not open this PR itself; the manager committed any",
            "leftover work (hooks enabled) and opened the PR so the lane could reach the gate.",
        ]
    )
    return "\n".join(lines)
