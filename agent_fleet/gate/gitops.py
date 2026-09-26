"""Git and GitHub plumbing for the gate: resolve a PR head, run the gate's worktrees.

The gate never operates on the caller's checkout. It creates a detached
worktree at the PR head so that a reviewer agent, a verifier writing a new test
file, and a fixer committing and pushing all work against a known, immutable
commit — and the operator's own working tree is never dirtied by a run.

Every worktree is created with ``--detach`` at an explicit sha. A gate run that
crashed mid-round leaves a directory behind but no branch to half-update, and
the next run removes and recreates it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - used at runtime (path.exists/is_file)

from agent_fleet.gate.pytest_runner import is_test_file

logger = logging.getLogger(__name__)


class GateError(RuntimeError):
    """A gate run cannot proceed (bad PR, unusable worktree, failed git call)."""


@dataclass(frozen=True)
class PullRequestRef:
    """The PR's head, as ``gh`` reports it."""

    number: int
    head_ref: str
    head_sha: str
    state: str
    base_ref: str = ""

    @property
    def short_sha(self) -> str:
        return self.head_sha[:9]

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"


def _run_git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command in *repo*; raise :class:`GateError` on failure."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"git {' '.join(args)} failed to launch: {exc}") from exc
    if check and completed.returncode != 0:
        raise GateError(
            f"git {' '.join(args)} exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()[:300]}"
        )
    return completed.stdout or ""


def resolve_pull_request(repo: Path, pr_number: int) -> PullRequestRef:
    """Resolve PR *pr_number* to its head ref/sha via the ``gh`` CLI.

    ``gh`` talks to the forge directly, so this works for a PR whose head
    branch lives in a fork and is not fetched locally.
    """
    if shutil.which("gh") is None:
        raise GateError("gh CLI not found; cannot resolve the PR head")
    try:
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "headRefName,headRefOid,state,baseRefName",
            ],
            capture_output=True,
            text=True,
            cwd=repo,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"gh pr view {pr_number} failed to launch: {exc}") from exc
    if completed.returncode != 0:
        raise GateError(
            f"gh pr view {pr_number} exited {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:300]}"
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise GateError(f"gh pr view {pr_number} returned unparseable JSON: {exc}") from exc
    return PullRequestRef(
        number=int(pr_number),
        head_ref=str(data.get("headRefName") or ""),
        head_sha=str(data.get("headRefOid") or ""),
        state=str(data.get("state") or ""),
        base_ref=str(data.get("baseRefName") or ""),
    )


def current_pr_head(repo: Path, pr_number: int) -> str:
    """Re-read the PR's head sha — how the gate notices a fixer's push."""
    return resolve_pull_request(repo, pr_number).head_sha


def prepare_worktree(repo: Path, path: Path, sha: str) -> Path:
    """Create a detached worktree at *sha*, replacing any previous one at *path*."""
    remove_worktree(repo, path)
    repo.parent.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "worktree", "add", "--detach", str(path), sha)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove a gate worktree. Never raises — a missing worktree is fine."""
    if not path.exists():
        _run_git(repo, "worktree", "prune", check=False)
        return
    _run_git(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)


def fetch_base(repo: Path, base_branch: str) -> None:
    """Fetch the base ref so ``git diff base...HEAD`` resolves in a fresh worktree."""
    _run_git(repo, "fetch", "--quiet", "origin", base_branch, check=False)
    _run_git(repo, "fetch", "--quiet", "origin", check=False)


def worktree_head_sha(worktree: Path) -> str:
    """The commit a gate worktree currently sits on (empty string on failure)."""
    return _run_git(worktree, "rev-parse", "HEAD", check=False).strip()


def resolve_diff_base(worktree: Path, base_branch: str) -> str:
    """The ref a PR diff is taken against: ``origin/<base>`` when it exists.

    The local ``<base>`` branch of the main checkout can be far behind the
    forge (seen: lake-of-rage local main hundreds of commits behind), and
    diffing against it pulls unrelated upstream files into "the PR's changes".
    :func:`fetch_base` refreshes ``origin/<base>`` first. Explicit remote refs
    and SHAs pass through; a repo with no ``origin`` falls back to the local branch.
    """
    explicit = base_branch.startswith(("origin/", "refs/"))
    if explicit or re.fullmatch(r"[0-9a-f]{7,40}", base_branch):
        return base_branch
    remote = f"origin/{base_branch}"
    probe = _run_git(worktree, "rev-parse", "--verify", "--quiet", remote, check=False).strip()
    return remote if probe else base_branch


def changed_test_files(worktree: Path, base_branch: str) -> list[str]:
    """Repo-relative ``test_*.py`` paths the PR changed and that still exist.

    Deliberately narrow: the gate re-runs the PR's *own* tests as step0, and
    widening that to the whole suite would turn one slow package into a gate
    timeout for reasons unrelated to the change.
    """
    diff = _run_git(
        worktree,
        "diff",
        "--name-only",
        f"{resolve_diff_base(worktree, base_branch)}...HEAD",
        check=False,
    )
    out: list[str] = []
    for line in diff.splitlines():
        rel = line.strip()
        if not rel or not is_test_file(rel):
            continue
        if (worktree / rel).is_file():
            out.append(rel)
    return out


# ---------------------------------------------------------------------------
# Patch identity: is this the same change, re-parented?
# ---------------------------------------------------------------------------

#: Shortest sha the gate matches an approval line by. The status line is written
#: at 9 characters and the automerge's regex accepts 7 to 40, so anything shorter
#: than the minimum is a prefix too weak to identify a commit: "123" matches
#: "1234abcd", which is a different one.
SHA_MIN_CHARS = 7

#: Where a gate-written test file lands: the test directory the verifier was told
#: to use, keyed on the lane-unique file name that ``gate_test_name`` produces.
#: Patch identity excludes that *path* rather than a name glob, because a glob
#: also hides any product code a contributor chose to call ``test_gate_*.py`` —
#: invisible new code under a name the gate itself chose.
_GATE_TEST_DIRS = (
    ":(exclude,glob)tests/test_gate_*.py",
    ":(exclude,glob)**/tests/test_gate_*.py",
)


def merge_base(repo: Path, a: str, b: str) -> str:
    """The common ancestor of *a* and *b* (empty string when there is none)."""
    return _run_git(repo, "merge-base", a, b, check=False).strip()


def merge_base_into(worktree: Path, base: str) -> None:
    """Merge *base* into the gate worktree, tolerating an already-merged base.

    A rebase means the PR has the base as an ancestor, so this is usually a
    no-op; it matters when the operator rebases by merging instead, or when the
    PR was approved before a commit landed on main. "Already merged" is not an
    error, so the exit code is not checked for that. Everything *else* is.

    A merge that did not happen is never a silent no-op. The three ways this
    bites, all of which used to return success:

    1. a *conflict*: the merge leaves conflict markers in the tree, and the
       deterministic half would then run against a tree no real merge produces
       — a file pytest cannot even import, or one that passes on a spliced
       result;
    2. an *unrelated history* (or any other ref git refuses to merge): the PR
       and the base share no ancestor, so nothing was merged at all;
    3. a *ref git cannot resolve* — a base branch that was never fetched, a
       deleted remote branch. The tests then run against the PR's own tree while
       the carry-over reports a verdict for a merge it never made.

    All three abort any half-finished merge and raise, which is the recheck
    refusing rather than reporting a verdict it never established.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(worktree), "merge", "--no-edit", base],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"git merge {base} failed to launch: {exc}") from exc
    if completed.returncode == 0:
        return
    output = (completed.stdout or "") + (completed.stderr or "")
    if "Already up to date" in output or "Already up-to-date" in output:
        # The base is already an ancestor: the merge is genuinely a no-op.
        return
    _run_git(worktree, "merge", "--abort", check=False)
    if "CONFLICT" in output or "Automatic merge failed" in output:
        raise GateError(
            f"git merge {base} conflicted: the PR and the base both change the same lines. "
            f"{(completed.stderr or completed.stdout).strip()[:300]}"
        )
    raise GateError(
        f"git merge {base} did not merge the base: exit {completed.returncode}. "
        f"{(completed.stderr or completed.stdout).strip()[:300]}"
    )


def patch_id(repo: Path, sha: str, base: str) -> str:
    """A content hash of *sha*'s change against *base*, ignoring gate tests.

    ``git patch-id`` hashes the diff itself, not the commit, so two commits
    carrying the same change hash the same even when their parents, authors and
    timestamps differ — which is exactly the "rebased onto a moved main" case
    an approval should survive. Only the gate's own test directory is dropped
    from the diff, so product code stays in the identity even when it is named
    like gate evidence. Returns ``""`` when the diff cannot be computed, so an
    unknown sha is never mistaken for "identical".
    """
    base_point = merge_base(repo, base, sha)
    if not base_point:
        return ""
    diff = _run_git(
        repo,
        "diff",
        base_point,
        sha,
        "--",
        ".",
        *_GATE_TEST_DIRS,
        check=False,
    )
    if not diff.strip():
        # An empty diff has no patch-id; treat it as "no change" rather than
        # letting an empty string compare equal to a real hash.
        return ""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "patch-id", "--stable"],
            input=diff,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"git patch-id failed to launch: {exc}") from exc
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").split()[0] if completed.stdout.split() else ""


def has_approval_line(status_file: Path, sha: str) -> bool:
    """Whether *status_file* records a ``PREMERGE-APPROVED`` line for *sha*.

    The same line contract the automerge reads, enforced by the automerge's own
    regex rather than re-derived: a reason quoting the marker inline, a trailing
    note after the sha, or anything but the marker followed by one hex sha can
    therefore never read as an approval. The sha is matched by prefix, because
    the status line is written at 9 characters — and only when it is at least
    that long, since a three-character prefix would match any sha starting with
    those three characters, which is a different commit. A missing file is not
    an approval.
    """
    from agent_fleet.fleet_ops.gate import _APPROVAL_LINE_RE, APPROVAL_MARKER

    if not status_file.is_file() or not sha or len(sha) < SHA_MIN_CHARS:
        return False
    try:
        lines = status_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    prefix = sha[:SHA_MIN_CHARS]
    for line in lines:
        if APPROVAL_MARKER not in line:
            continue
        match = _APPROVAL_LINE_RE.match(line.strip())
        if match is not None and match.group(0).split()[-1].startswith(prefix):
            return True
    return False
