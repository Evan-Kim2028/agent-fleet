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


def _run_git_completed(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in *repo*; raise :class:`GateError` if it cannot launch.

    The *completed* process is returned rather than its stdout so that a caller
    can tell a command that ran and failed from one that produced no output:
    ``git diff`` against a base ref that cannot be resolved exits non-zero with
    empty stdout, and reading only stdout makes that identical to a diff that is
    genuinely empty.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateError(f"git {' '.join(args)} failed to launch: {exc}") from exc


def _run_git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command in *repo*; raise :class:`GateError` on failure."""
    completed = _run_git_completed(repo, *args)
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


def resolve_base_branch(pull_request: PullRequestRef | None, configured: str) -> str:
    """The base the gate diffs: the PR's own base, or *configured* as the default.

    A stacked PR bases itself on another feature branch, and ``main`` is the
    wrong yardstick for it in both directions: every commit the base branch took
    since the fork lands in the diff as if this PR had made it, and the base
    branch's own new work makes this PR look far bigger than it is — which is
    exactly what inflates a diff until the reviewer never reaches the end of it.
    The forge already knows which branch this PR targets, so the gate asks it
    rather than assuming.

    *configured* is only the fallback, taken whenever the PR is unknown or
    reports no base (a ``gh`` failure, a local-only ref): a missing base must
    never become a diff against nothing. An explicit ``origin/...``, ``refs/...``
    or sha in the config is the operator speaking directly and is passed through
    untouched.
    """
    if configured.startswith(("origin/", "refs/")) or re.fullmatch(r"[0-9a-f]{7,40}", configured):
        return configured
    return ((pull_request.base_ref if pull_request else "") or "").strip() or configured


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
# The change, inline
# ---------------------------------------------------------------------------

#: Context lines around each hunk. Wide enough that a reviewer can see the
#: guard it is about to reason about breaking, narrow enough that a whole-file
#: rewrite of a small file does not triple the prompt.
DIFF_CONTEXT_LINES = 25

#: What the reviewer prompt never needs to see. Tests are the verifier's
#: evidence and the deterministic half's business — a reviewer reporting a
#: failing test is just describing step0 — and markdown cannot be a blocker, so
#: both only inflate the prompt with text carrying no review signal.
#:
#: The wildcards are chosen against git's two different matching rules, because
#: the obvious spelling of each is the one that silently does nothing:
#:
#: - without ``glob``, ``*`` **does** cross a ``/``, so ``:(exclude)*.md`` alone
#:   matches ``docs/CHANGELOG.md`` as well as ``README.md``. Adding ``glob`` to
#:   that same pattern *stops* it matching at depth, and ``:(exclude,glob)*.md``
#:   then leaves every nested markdown in the diff.
#: - a directory needs a trailing ``/**``, and a bare ``:(exclude)tests/`` only
#:   ever matches the repository's root ``tests`` — never ``api/tests``. The
#:   ``**/`` prefix is what makes the sub-package case work, and it is what the
#:   equivalent gate test directory rules elsewhere in this module already use.
#:
#: ``.agent-fleet/`` is machine-local run state — the same transcripts and
#: per-PR notes that .gitignore says must never be committed. A run transcript is
#: megabytes of raw model thinking deltas, tool inputs and outputs, and it
#: grows on every dispatch, so one of them landing in a diff does not merely
#: add noise: at the 150k cap it fills the whole brief and evicts every line of
#: the actual code change, leaving the reviewers to review a JSON event log and
#: return no blockers. It is excluded rather than only ignored because .gitignore
#: is advisory — a tracked file stays tracked — while this is the gate's own
#: guarantee about what a reviewer is ever handed.
_DIFF_EXCLUDES: tuple[str, ...] = (
    ":(exclude)**/test_*.py",
    ":(exclude)**/*_test.py",
    ":(exclude)**/tests/**",
    ":(exclude)*.md",
    ":(exclude).agent-fleet/**",
    ":(exclude).agent-fleet-state.json",
)


#: Marker at the head of the note :attr:`InlineDiff.note` produces when the diff
#: could not be computed. Exported so the prompt can branch on the note without
#: re-deriving the wording, and so the two halves cannot drift apart.
DIFF_NOT_COMPUTED = "(NOT COMPUTED"


@dataclass(frozen=True)
class InlineDiff:
    """The change a reviewer is handed, and whether all of it is here."""

    text: str
    truncated: bool
    #: The diff could not be computed at all — an unresolvable base ref, a
    #: repository state git refuses. Distinct from an empty change, which is a
    #: real answer: "this PR changes nothing". Both carry no text, so without
    #: this flag the reviewer is handed nothing and told it is the whole change.
    failed: bool = False

    @property
    def note(self) -> str:
        """The line the prompt shows instead of leaving a cut diff unremarked.

        A silently clipped diff is the worst outcome available to a reviewer: it
        reads as the complete change and the half that was dropped is never
        reviewed. The prompt therefore always says which of the two it got, so a
        reviewer can spend a tool call on the remainder when the cap bit.

        A diff that could not be computed says so rather than reporting a
        complete empty one: it is not the same answer, and the difference is the
        whole verdict — a clean review of nothing is not a clean review.
        """
        if self.failed:
            return (
                "(NOT COMPUTED: git diff against this base failed — the change is "
                "NOT empty and NOT reviewed; do not report this as a clean review)"
            )
        if not self.truncated:
            return "(complete)"
        return f"(TRUNCATED at {len(self.text)} chars: run git diff for the rest)"


def inline_change(worktree: Path, base_branch: str, *, max_chars: int) -> InlineDiff:
    """The PR's change, diffed against *base_branch* and capped at *max_chars*.

    The diff is computed with the *merge base* semantics of ``git diff A...B``,
    so a stacked PR shows only its own commits and never the base branch's
    movement underneath them.

    The cap is applied to the text handed to the model, not to git: cutting
    inside a hunk is acceptable, and it is recorded in :attr:`InlineDiff.note`
    so the reviewer knows the change is partial rather than quietly reviewing
    half a PR.

    A diff git *refuses* to produce is reported as :attr:`InlineDiff.failed`, not
    as an empty change. The forge reports a base branch that may since have been
    deleted, renamed, or never fetched; every one of those exits non-zero with
    empty stdout, exactly what a PR with no changes produces. Reporting the
    second as the first is the failure the note exists to prevent: the reviewer
    is pointed at a diff that cannot exist, spends its whole budget re-deriving
    it, and returns no findings — a clean review of nothing.
    """
    base = resolve_diff_base(worktree, base_branch)
    completed = _run_git_completed(
        worktree,
        "diff",
        f"-U{DIFF_CONTEXT_LINES}",
        f"{base}...HEAD",
        "--",
        ".",
        *_DIFF_EXCLUDES,
    )
    if completed.returncode != 0:
        return InlineDiff(text="", truncated=False, failed=True)
    diff = completed.stdout or ""
    if not diff:
        return InlineDiff(text="", truncated=False)
    cap = max(int(max_chars), 0)
    if cap and len(diff) > cap:
        return InlineDiff(text=diff[:cap], truncated=True)
    return InlineDiff(text=diff, truncated=False)


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
