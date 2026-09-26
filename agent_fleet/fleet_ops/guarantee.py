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
``--no-verify`` is never used. The *only* concession is a ``SKIP=`` environment
overlay naming the hook ids the repo declared as baseline-red — pre-commit's
own supported mechanism, and narrower than disabling hooks. If a non-baseline
hook fails, the commit fails and the lane escalates with the hook output
attached, which is the correct outcome: that is a real problem with the diff.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    engine: str, *, task_file: str | None = None, lane: str | None = None
) -> str:
    """Commit message for the manager's auto-commit.

    Subject is the fixed ``fleet: auto-commit after <engine> run`` convention.
    The body records provenance, so a reviewer seeing this commit in the PR
    history knows the agent did not make it and where the work came from.
    """
    subject = AUTO_COMMIT_SUBJECT.format(engine=engine)
    lines = [subject, ""]
    lines.append("Committed by the agent-fleet lane manager: the implementer left these")
    lines.append("changes in the worktree without committing them.")
    if lane:
        lines.append(f"Lane: {lane}")
    if task_file:
        lines.append(f"Task file: {task_file}")
    return "\n".join(lines)


def _stage_lane_work(
    worktree: Path, *, env: dict[str, str] | None = None, runner: Runner | None = None
) -> tuple[bool, str]:
    """Stage the lane's work, leaving the engine's run logs out of the index.

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
    run-log subtree is then taken back out of the index. Unstaging also covers
    a *tracked* transcript, which an exclusion pathspec could not, and
    ``info/exclude`` still governs the untracked half: a run log that no repo
    tracks never reaches the index at all.
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

    # `git reset` (not `rm --cached`) so an *edit* to a tracked run log is
    # unstaged back to HEAD rather than turned into a deletion. A path that is
    # not in the index is a no-op here, so this never fails on a clean lane.
    unstage = _git(
        ["git", "reset", "-q", "--", RUN_DIR_LOGS],
        cwd=worktree,
        runner=runner,
        env=env,
        timeout=300,
    )
    if unstage.returncode != 0:
        return False, f"git reset failed: {(unstage.stderr or unstage.stdout).strip()[:500]}"
    return True, ""


def commit_worktree(
    worktree: Path,
    *,
    engine: str,
    task_file: str | None = None,
    lane: str | None = None,
    skip_hooks: Sequence[str] = (),
    runner: Runner | None = None,
) -> tuple[bool, str | None, str, list[str]]:
    """Stage everything *except the run logs* and commit with hooks enabled.

    Returns ``(committed, sha, detail, hooks_failed)``. *skip_hooks* becomes the
    ``SKIP=`` environment overlay — pre-commit's own selective-skip mechanism.
    Hooks not named there run normally; a failure from one of them aborts the
    commit, and both the ids of the failing hooks and the output are surfaced so
    the lane can escalate with something actionable.

    The run logs are kept out twice over: ``info/exclude`` (written when the
    worktree was set up) and the explicit filtering in :func:`_stage_lane_work`
    — an explicit ``--run-dir`` inside the worktree, or a repo whose exclude
    file could not be written, must not be able to put a transcript into a
    commit.
    """
    skip_env = {"SKIP": ",".join(h for h in skip_hooks if h)} if any(skip_hooks) else {}
    # Overlay SKIP on the real environment: a bare {"SKIP": ...} env strips PATH/HOME and
    # breaks the very hooks the manager promises to keep live.
    env = {**os.environ, **skip_env} if skip_env else None

    staged, stage_detail = _stage_lane_work(worktree, env=env, runner=runner)
    if not staged:
        return False, None, stage_detail, []

    message = build_commit_message(engine, task_file=task_file, lane=lane)
    commit = _git(
        ["git", "commit", "-m", message],
        cwd=worktree,
        runner=runner,
        env=env,
        timeout=900,
    )
    if commit.returncode != 0:
        detail = "\n".join(p for p in (commit.stdout, commit.stderr) if p).strip()[:2000]
        return False, None, detail or "git commit failed", failed_hook_ids(detail)

    return True, head_sha(worktree, runner=runner), "", []


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
    if is_dirty(worktree, runner=runner):
        ok, sha, detail, hooks_failed = commit_worktree(
            worktree,
            engine=engine,
            task_file=task_file,
            lane=lane,
            skip_hooks=tuple(skip_env.get("SKIP", "").split(",")) if skip_env.get("SKIP") else (),
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
            reason="no_changes_stopped" if no_changes_detail else "no_commits_ahead",
            escalated=True,
            detail=f"{summary}\n{no_changes_detail}".strip() if no_changes_detail else summary,
        )

    pr = _create_pr(
        branch=branch,
        base=base,
        title=title or _default_pr_title(lane=lane, engine=engine, task_file=task_file),
        body=body or _default_pr_body(lane=lane, engine=engine, task_file=task_file),
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


def _default_pr_body(*, lane: str | None, engine: str, task_file: str | None) -> str:
    lines = [
        "Opened automatically by the agent-fleet lane manager.",
        "",
        f"**Engine:** `{engine}`",
    ]
    if lane:
        lines.append(f"**Lane:** `{lane}`")
    if task_file:
        lines.append(f"**Task file:** `{task_file}`")
    lines.extend(
        [
            "",
            "The implementer did not open this PR itself; the manager committed any",
            "leftover work (hooks enabled) and opened the PR so the lane could reach the gate.",
        ]
    )
    return "\n".join(lines)
