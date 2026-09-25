"""Repo and PR binding — the check that stops a lane judging the wrong PR.

A stray ``REVIEW_REPO``/``PR`` environment variable once sent lake-of-rage PR
#3544 to silphcoanalytics PR #3544: same PR number, two different repos, and
four review lenses started on the wrong code before a human noticed. Nothing was
wrong with the code, only with *which* repository the lane was pointed at.

Two facts have to hold before any gate or reviewer runs, and both are derived
here from the lane's own worktree rather than from anything inherited:

1. **The repo comes from the worktree's ``origin`` remote.** A lane that lives in
   ``~/Documents/silphcoanalytics-wt-fb-foo`` is a silphcoanalytics lane, full
   stop. The main working tree's slug, the lane name, and the environment are all
   untrusted.
2. **The PR's ``headRefName`` must be the lane's branch.** A PR whose head is a
   different branch is not this lane's PR. Following it would let a lane's gate
   approve or fix *someone else's* branch.

:meth:`LaneBinding.resolve` returns the binding or refuses with a reason. Every
refusal is a hard error, never a warning: the safe action is to not run.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    # Annotations only — every use site is a string under `from __future__`.
    Runner = Callable[..., subprocess.CompletedProcess[str]]

logger = logging.getLogger(__name__)

#: Refusal reasons, all of which mean "do not run the gate".
REFUSED_NO_ORIGIN = "refused_no_origin_remote"
REFUSED_SLUG_MISMATCH = "refused_repo_slug_mismatch"
REFUSED_HEAD_MISMATCH = "refused_pr_head_mismatch"
REFUSED_NO_PR = "refused_no_pr_for_branch"
REFUSED_PR_LOOKUP_FAILED = "refused_pr_lookup_failed"


@dataclass(frozen=True)
class LaneBinding:
    """The verified (worktree, repo slug, branch, PR) tuple for a lane."""

    repo_slug: str
    branch: str
    pr: int
    head_ref: str
    worktree: Path
    #: The PR's head commit (``headRefOid``) at resolve time, if gh returned it.
    head_sha: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "repo_slug": self.repo_slug,
            "branch": self.branch,
            "pr": self.pr,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
            "worktree": str(self.worktree),
        }


@dataclass(frozen=True)
class BindingResult:
    """Either a verified binding, or a refusal with a reason."""

    binding: LaneBinding | None
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.binding is not None


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    runner: Runner | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    run = runner or subprocess.run
    return run(list(args), cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)


def origin_slug(worktree: Path, *, runner: Runner | None = None) -> str | None:
    """The ``owner/repo`` slug of *worktree*'s ``origin`` remote, or None.

    Read from the worktree, never from ``gh``'s ambient repo guess: the whole
    point is that the worktree is the authority on which repo this lane is in.
    """
    result = _run(["git", "remote", "get-url", "origin"], cwd=worktree, runner=runner, timeout=60)
    if result.returncode != 0:
        return None
    return parse_remote_slug(result.stdout.strip())


def parse_remote_slug(url: str) -> str | None:
    """Extract ``owner/repo`` from an SSH or HTTPS remote URL.

    Handles the four shapes git actually produces::

        git@github.com:owner/repo.git
        ssh://git@github.com/owner/repo.git
        https://github.com/owner/repo.git
        https://github.com/owner/repo

    Returns None rather than guessing when the URL carries no owner/repo path. A
    host-only URL like ``https://github.com/onlyowner`` must not yield
    ``github.com/onlyowner``: that names the *host* as the owner, and the result
    then gets compared against real slugs and can match the wrong repository.
    """
    text = (url or "").strip()
    if not text:
        return None
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]

    if "//" in text:
        scheme, _, rest = text.partition("//")
        if not scheme.startswith(("http", "ssh", "git")):
            return None
        _authority, _, path = rest.partition("/")
        parts = [p for p in path.split("/") if p]
    elif ":" in text:
        # scp-like: host:owner/repo
        _host, _, tail = text.partition(":")
        parts = [p for p in tail.split("/") if p]
    else:
        return None

    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def find_pr(
    worktree: Path, branch: str, *, runner: Runner | None = None
) -> dict[str, object] | None:
    """The open PR whose head is *branch*, with its ``headRefName`` and ``headRefOid``.

    Queries with an explicit ``--head <branch>`` so the lookup is already scoped;
    the returned head is then verified rather than trusted.
    """
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,headRefName,headRefOid,baseRefName",
            "--limit",
            "1",
        ],
        cwd=worktree,
        runner=runner,
        timeout=120,
    )
    if result.returncode != 0:
        logger.warning("gh pr list failed in %s: %s", worktree, result.stderr[:300])
        return None
    try:
        items = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not items or not isinstance(items, list):
        return None
    first = items[0]
    return first if isinstance(first, dict) else None


def resolve(
    worktree: Path,
    *,
    branch: str,
    expected_slug: str | None = None,
    runner: Runner | None = None,
) -> BindingResult:
    """Verify the lane's (repo, branch, PR) binding, or refuse.

    *expected_slug*, when given, is an additional assertion — the operator
    session's own understanding of which repo it is driving. A mismatch refuses
    rather than "correcting" it, because the operator is the one who knows which
    session they are in.
    """
    worktree = Path(worktree)
    if not worktree.is_dir():
        return BindingResult(None, REFUSED_NO_ORIGIN, f"no worktree at {worktree}")

    slug = origin_slug(worktree, runner=runner)
    if not slug:
        return BindingResult(
            None,
            REFUSED_NO_ORIGIN,
            f"could not read an origin remote from {worktree}; refusing to guess the repo",
        )

    if expected_slug and slug.lower() != expected_slug.strip().lower():
        return BindingResult(
            None,
            REFUSED_SLUG_MISMATCH,
            f"worktree {worktree} is bound to {slug}, not the expected {expected_slug}",
        )

    pr = find_pr(worktree, branch, runner=runner)
    if pr is None:
        return BindingResult(
            None,
            REFUSED_NO_PR,
            f"no open PR for {slug} head {branch}",
        )

    head_ref = str(pr.get("headRefName") or "")
    if head_ref != branch:
        return BindingResult(
            None,
            REFUSED_HEAD_MISMATCH,
            f"PR #{pr.get('number')} in {slug} has headRefName {head_ref!r}, expected {branch!r}",
        )

    number = pr.get("number")
    if not isinstance(number, int):
        return BindingResult(
            None,
            REFUSED_PR_LOOKUP_FAILED,
            f"gh returned a PR for {slug} head {branch} with a non-numeric number {number!r}",
        )

    return BindingResult(
        LaneBinding(
            repo_slug=slug,
            branch=branch,
            pr=number,
            head_ref=head_ref,
            head_sha=str(pr.get("headRefOid") or ""),
            worktree=worktree,
        )
    )


def gate_env(binding: LaneBinding) -> dict[str, str]:
    """Environment for a gate subprocess, pinned to the verified binding.

    Every repo-identifying variable is set from the *verified* values, and the
    ambient ones are overwritten rather than merely added to. A gate subprocess
    that somehow inherited a stale ``REVIEW_REPO`` cannot act on it.
    """
    return {
        "REPO_SLUG": binding.repo_slug,
        "REPO": binding.repo_slug,
        "REVIEW_REPO": binding.repo_slug,
        "PR": str(binding.pr),
        "PR_NUMBER": str(binding.pr),
        "BRANCH": binding.branch,
        "HEAD_REF": binding.head_ref,
        "HEAD_SHA": binding.head_sha,
        "WORKTREE": str(binding.worktree),
    }


__all__ = [
    "REFUSED_HEAD_MISMATCH",
    "REFUSED_NO_ORIGIN",
    "REFUSED_NO_PR",
    "REFUSED_PR_LOOKUP_FAILED",
    "REFUSED_SLUG_MISMATCH",
    "BindingResult",
    "LaneBinding",
    "find_pr",
    "gate_env",
    "origin_slug",
    "parse_remote_slug",
    "resolve",
]
