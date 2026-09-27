"""Git and GitHub plumbing for the gate: resolve a PR head, run the gate's worktrees.

The gate never operates on the caller's checkout. It creates a detached
worktree at the PR head so that a reviewer agent, a verifier writing a new test
file, and a fixer committing and pushing all work against a known, immutable
commit — and the operator's own working tree is never dirtied by a run.

Every worktree is created with ``--detach`` at an explicit sha. A gate run that
crashed mid-round leaves a directory behind but no branch to half-update, and
the next run removes and recreates it.

**Worktree mutation is serialized per repository** (:func:`worktree_lock`), and
under *the same lock every other subsystem takes*
(:func:`agent_fleet.fleet_ops.worktree_lock.repo_worktree_lock`).
``git worktree add`` and ``git worktree prune`` are not safe to run concurrently
against one repository: prune deletes the administrative entries under
``.git/worktrees`` for directories it cannot find, including a sibling's
worktree that ``add`` has registered but not yet finished populating. The
observed failure was ``fatal: could not write new index file`` / ``could not open
'.git/worktrees/<name>/locked' for writing: No such file or directory``, which
loses the sibling's worktree outright. The lock covers add, remove, *and* prune
together — locking only ``add`` would still let one gate's prune run while
another's add is mid-flight, which is the actual corruption.

One lock per repository is the whole point: a gate lock anywhere but the
shared one is a second flock over one shared resource, and a lane ``add`` would
still interleave with this gate's ``prune``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - used at runtime (path.exists/is_file)
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.binding import parse_remote_slug
from agent_fleet.fleet_ops.worktree_lock import repo_key, repo_worktree_lock
from agent_fleet.gate.pytest_runner import is_test_file

if TYPE_CHECKING:
    from agent_fleet.gate.config import GateConfig

logger = logging.getLogger(__name__)


class GateError(RuntimeError):
    """A gate run cannot proceed (bad PR, unusable worktree, failed git call)."""


class GateTargetMismatch(GateError):
    """The caller's named repo or head is not the one under review.

    Distinct from :class:`GateError` because it is a *caller* error rather than
    a gate-infrastructure one: the PR is fine, but the gate was pointed at the
    wrong thing and refusing is the only safe answer.
    """


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


def origin_slug(repo: Path) -> str:
    """``owner/name`` for *repo*'s own ``origin``, or "" when it has no remote.

    Read from the checkout rather than from a caller-supplied name, so the
    cross-check below compares the repo under review against the repo that
    remote actually points at.

    Parsing is delegated to
    :func:`~agent_fleet.fleet_ops.binding.parse_remote_slug`, the single parser
    the worktree lock already uses. Re-deriving it here mis-read the scp-style
    form ``git@github.com:owner/repo.git``: the ``@`` sits before a ``:`` and not
    before a ``/``, so the userinfo was never stripped and the result came back
    as ``github.com:owner/repo``. The cross-check then refused every SSH
    checkout's own origin as a mismatch. A host-only remote yields "" rather
    than a slug naming the host as the owner, which the parser also declines.
    """
    url = _run_git(repo, "config", "--get", "remote.origin.url", check=False).strip()
    if not url:
        return ""
    return parse_remote_slug(url) or ""


def cross_check_gate_target(
    repo_path: Path,
    pr_number: int,
    *,
    repo: str | None = None,
    head_ref: str | None = None,
) -> None:
    """Raise :class:`GateTargetMismatch` when the caller's named target is wrong.

    *repo* is the slug the caller believes it is gating and *head_ref* the
    branch it believes the PR head is on. Both are asserted against what the
    checkout and the forge actually say, so a gate can never be pointed at
    another team's PR, or at a stale head, without saying so.

    A caller that names neither is asserting nothing and is not checked: the
    gate resolves the PR itself in that case, exactly as it always did.
    """
    if not repo and not head_ref:
        return
    if repo:
        actual = origin_slug(repo_path)
        if actual and actual.lower() != repo.strip().lower():
            raise GateTargetMismatch(
                f"--repo {repo!r} is not this checkout's origin (which is {actual!r})"
            )
    if head_ref:
        actual_ref = resolve_pull_request(repo_path, pr_number).head_ref
        if actual_ref and actual_ref != head_ref:
            raise GateTargetMismatch(
                f"--head-ref {head_ref!r} is not PR #{pr_number}'s head (which is {actual_ref!r})"
            )


def _repo_key(repo: Path) -> str:
    """A stable, credential-free name for the repository at *repo*.

    Delegates to :func:`agent_fleet.fleet_ops.worktree_lock.repo_key` so the
    gate and the admission lock index cannot disagree about a repository's
    name. The raw ``remote.origin.url`` is never used: after ``gh auth
    setup-git`` it carries a live token, and a token must not end up in a
    filename.
    """
    return repo_key(repo)


def _lock_dir() -> Path:
    """``~/.agent-fleet/admission/locks``, where the worktree lock is indexed.

    The gate no longer *holds* anything here. A ``flock`` is visible to every
    process on the box, so the lock file has to outlive a crashed holder, and a
    file inside a gate worktree would be deleted out from under its holder the
    moment ``git worktree remove`` took that worktree's administrative
    directory with it. The lock therefore lives at
    ``<git-dir>/agent-fleet-worktree.lock`` — see
    :func:`agent_fleet.fleet_ops.worktree_lock.repo_worktree_lock` — and this
    directory keeps a pointer to it per repository.
    """
    from agent_fleet.fleet_ops.admission import AdmissionConfig

    return AdmissionConfig().locks_dir()


def worktree_lock(repo: Path):  # noqa: ANN201
    """Serialize worktree add/remove/prune for *repo* across processes and threads.

    Delegates to
    :func:`agent_fleet.fleet_ops.worktree_lock.repo_worktree_lock`, which is
    what :func:`agent_fleet.fleet_ops.worktree.ensure_lane_worktree` and the
    merge-plan probe take: one lock per repository, shared by every subsystem
    that mutates ``.git/worktrees``. In-process it serializes threads and is
    re-entrant per thread (``prepare_worktree`` calls ``remove_worktree``);
    across processes it is an ``flock``, which is the case that actually lost
    worktrees, since gates are separate processes.

    Degrades to running unlocked if the lock file cannot be created: a gate that
    cannot take a lock is still better than a gate that refuses to run.
    """
    return repo_worktree_lock(repo)


def prepare_worktree(repo: Path, path: Path, sha: str) -> Path:
    """Create a detached worktree at *sha*, replacing any previous one at *path*."""
    with worktree_lock(repo):
        remove_worktree(repo, path)
        repo.parent.mkdir(parents=True, exist_ok=True)
        _run_git(repo, "worktree", "add", "--detach", str(path), sha)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove a gate worktree. Never raises — a missing worktree is fine."""
    with worktree_lock(repo):
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


def changed_paths(worktree: Path, base_branch: str) -> list[str]:
    """Repo-relative paths the PR changed, or ``[]`` when the diff is unreadable.

    Shared by the two tiering questions — "is this PR docs/tests only" and "does
    it touch production-sensitive files" — so both see the same file list and a
    git failure reads as "no changed files" rather than a different answer from
    each caller. The tiering paths that would approve on an empty list refuse,
    so failing closed here is safe.
    """
    diff = _run_git(
        worktree,
        "diff",
        "--name-only",
        f"{resolve_diff_base(worktree, base_branch)}...HEAD",
        check=False,
    )
    return [line.strip() for line in diff.splitlines() if line.strip()]


def changed_test_files(worktree: Path, base_branch: str) -> list[str]:
    """Repo-relative ``test_*.py`` paths the PR changed and that still exist.

    Deliberately narrow: the gate re-runs the PR's *own* tests as step0, and
    widening that to the whole suite would turn one slow package into a gate
    timeout for reasons unrelated to the change.
    """
    out: list[str] = []
    for rel in changed_paths(worktree, base_branch):
        if not is_test_file(rel):
            continue
        if (worktree / rel).is_file():
            out.append(rel)
    return out


def deleted_test_paths(worktree: Path, base_branch: str) -> list[str]:
    """Repo-relative ``test_*.py`` paths the PR removed.

    :func:`changed_test_files` cannot report these: it keeps only the paths that
    still exist, so a PR whose change is a deletion has an empty step0 set, no
    run happens, and there is no failure to record. The approval tier built on
    step0 then rests on a green run of nothing, so the removal has to be
    readable on its own.
    """
    diff = _run_git(
        worktree,
        "diff",
        "--diff-filter=D",
        "--name-only",
        f"{resolve_diff_base(worktree, base_branch)}...HEAD",
        check=False,
    )
    return [line.strip() for line in diff.splitlines() if is_test_file(line.strip())]


#: Suite-level test configuration: not a ``test_*.py``, so never step0-runnable,
#: and the file pytest reads before it collects anything. Skipped, xfailed or
#: turned off wholesale, it decides what the suite even executes.
_TEST_CONFIG_RE = re.compile(r"(^|/)conftest\.py$|(^|/)(pytest\.ini|tox\.ini|setup\.cfg)$")


def is_test_config(path: str) -> bool:
    """Whether *path* is suite-level test configuration rather than a test."""
    return _TEST_CONFIG_RE.search(path) is not None


def changed_test_config_paths(worktree: Path, base_branch: str) -> list[str]:
    """Changed suite-level test-config paths, whatever the gate's test selector says."""
    return [path for path in changed_paths(worktree, base_branch) if is_test_config(path)]


# ---------------------------------------------------------------------------
# Review tiering: how much review a diff is worth
# ---------------------------------------------------------------------------

#: A changed path that carries no product behaviour: prose, test code, fixtures.
#: Tier 0 is defined as *only* these, and the non-test line count excludes them.
#: The patterns are matched against the whole repo-relative path.
_DOCS_TEST_RE = re.compile(
    r"(\.md$|(^|/)docs?/|(^|/)tests?/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)fixtures?/)"
)

#: Non-test changed lines, which is what ``big_lines`` is measured against.
#: Wider than :data:`_DOCS_TEST_RE` on purpose: a JSON fixture or a snapshot is
#: as much diff bulk as a test file and as little review risk.
_NON_TEST_LINE_RE = re.compile(
    r"((^|/)(tests?|fixtures?|docs?)/|(^|/)test_[^/]*\.py$|_test\.py$|\.md$|\.snap$|\.json$)"
)


def is_docs_or_test(path: str) -> bool:
    """Whether *path* is prose, a test, or a fixture — no product code."""
    return _DOCS_TEST_RE.search(path) is not None


def diff_line_stats(worktree: Path, base_branch: str) -> int:
    """Non-test changed lines in the PR (added + deleted).

    Test, fixture, docs, snapshot and JSON paths are excluded: they inflate a
    diff without adding review risk, and counting them put almost every PR over
    the size threshold that earns the full lens set. Binary files report no
    numstat line, so they add nothing.
    """
    numstat = _run_git(
        worktree,
        "diff",
        "--numstat",
        f"{resolve_diff_base(worktree, base_branch)}...HEAD",
        check=False,
    )
    total = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or _NON_TEST_LINE_RE.search(parts[2]):
            continue
        try:
            total += int(parts[0]) + int(parts[1])
        except ValueError:
            # "-" in either column means a binary file; it has no line count.
            continue
    return total


def prodsensitive_paths(worktree: Path, base_branch: str, config: GateConfig) -> list[str]:
    """Changed paths *config* considers production-sensitive, in diff order."""
    return [path for path in changed_paths(worktree, base_branch) if config.is_prodsensitive(path)]


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
