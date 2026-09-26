"""Land approved PRs in batches, testing the *combination* once.

With ~100 open agent PRs against one moving main, the expensive path is
merge -> conflict -> rebase -> re-gate, repeated one PR at a time.  Most of
today's gate failures are "fails once main is merged in": a conflict, or a
regression that only shows up alongside the other work.  The fix is to answer
that question once for the whole batch instead of once per PR.

One run, for one repository
---------------------------
1. **Order** the approved PRs oldest first, respecting stacks: a PR whose base
   branch is another PR's head branch lands after that PR (:func:`order_batch`).
   The order is a function of the batch's *contents* alone, so a batch collected
   in a different order still folds and merges identically.
2. **Fold** each PR head into a candidate branch built from the base branch
   with ``git merge --no-ff``, in a throwaway worktree so the operator's
   checkout is never touched.  A PR that conflicts is set aside as
   ``NEEDS-REBASE`` and the fold continues without it — one conflicted PR must
   not hold back work that has nothing to do with it.  The heads are fetched
   first, and a head the checkout cannot hold at all is reported as
   ``UNFETCHABLE`` rather than mistaken for a conflict.  The base branch is the
   one the batch's root PRs, the config, or the remote say
   (:func:`resolve_base_branch`), not a literal ``main`` — a stacked PR's base
   names its parent in the same batch and is folded onto the candidate, so only
   the roots are asked which branch the batch merges into.
3. **Test once** on the combined tree.  A green batch lands as a unit and
   returns a single result, so the caller deploys once.
4. **On red, bisect**: split the batch, re-test each half, and keep only the
   failing half.  The PRs found at fault are reported as ``REGRESSION`` with
   the failing test ids, and the rest of the batch still lands.

The seam that makes this testable
---------------------------------
:class:`MergeTrain` takes one injected callable, ``evaluate(batch)``, which
builds the combined tree for exactly those PRs and tests it.  Ordering,
conflict set-aside, bisection, and bookkeeping are the parts that can fail in
quiet ways, so :meth:`MergeTrain.run` exercises all of them against fakes in
the unit tests — no git, no network, no clock.  :class:`GitTrainer` is the real
implementation, and ``run_train`` is the git/gh entry point the CLI calls.

Safety
------
No force pushes and no ``--no-verify``: hooks keep running.  Every merge is
``gh pr merge --merge``, never squash or rebase, so the history the train
tested is the history that lands.  A PR whose head moved since the gate
approved it is dropped before the batch is built, and re-checked immediately
before its merge — merging unreviewed commits is exactly what the gate exists
to prevent.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_fleet.fleet_ops.worktree_lock import repo_worktree_lock
from agent_fleet.fleet_paths import agent_fleet_home

logger = logging.getLogger(__name__)

#: Verdicts a run can reach for one PR.
LANDED = "LANDED"
NEEDS_REBASE = "NEEDS-REBASE"
REGRESSION = "REGRESSION"
SKIPPED_MOVED = "SKIPPED-MOVED"
UNFETCHABLE = "UNFETCHABLE"

#: Branch the train folds onto when nothing says otherwise.  It is a fallback
#: and nothing more: :func:`resolve_base_branch` reaches for the remote's own
#: answer before this is ever read, so a repository that does not call its
#: default branch ``main`` is folded onto the right commits.
DEFAULT_BASE_BRANCH = "main"

#: Ceiling on one test invocation, in seconds.  A batch test that hangs must
#: not hold the train open forever, so it is killed and counted red.
DEFAULT_TEST_TIMEOUT = 1800

#: Change paths treated as tests, and the sibling name a module's test would
#: have, so a change to ``foo/bar.py`` also runs ``foo/test_bar.py``.
_TEST_SUFFIXES = ("test_", "_test")


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainPR:
    """One PR the gate approved, and the facts the train acts on.

    ``head_sha`` is the approved head the train merges.  ``base_ref`` and
    ``head_branch`` are the two ends of the stack: a PR whose ``base_ref`` names
    another PR's ``head_branch`` has to land after it or its base will not
    exist.  ``current_head`` is where the head points at *now*; when it
    disagrees with ``head_sha`` the gate never reviewed those commits and the
    PR is dropped.
    """

    number: int
    head_sha: str
    base_ref: str = ""
    head_branch: str = ""
    current_head: str = ""
    title: str = ""
    files: tuple[str, ...] = ()
    #: Fetch ref for this head, e.g. ``refs/pull/42/head``.  It is what makes
    #: the head materialisable from a fork, where its branch name resolves to
    #: nothing on the base repository.
    head_ref: str = ""

    @property
    def moved(self) -> bool:
        """True when the head has moved since the gate approved it.

        Compared as a prefix, not for equality: a gate approval line records a
        *short* SHA (``PREMERGE-APPROVED dc91fac``) while GitHub reports the
        full 40-character head, so an exact comparison would read every
        perfectly good PR as moved and drop the whole batch.
        """
        if not self.current_head:
            return False
        return not self.current_head.startswith(self.head_sha)

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "head_sha": self.head_sha,
            "base_ref": self.base_ref,
            "head_branch": self.head_branch,
            "current_head": self.current_head,
            "moved": self.moved,
            "title": self.title,
        }


@dataclass(frozen=True)
class TestResult:
    """The verdict of one build-and-test over one set of PRs.

    ``conflicts`` reports PRs the fold could not apply.  It is a field rather
    than a failure because a conflict is not a test outcome: the caller sets
    those PRs aside and tests the rest, rather than bisecting for a bug that is
    a merge conflict.  ``unfetchable`` is the same kind of separation for the
    other thing a fold can fail to do — a head the checkout has no object for,
    which is a broken checkout, not a stale branch and not a test result.
    """

    passed: bool
    failing: tuple[str, ...] = ()
    summary: str = ""
    conflicts: tuple[int, ...] = ()
    unfetchable: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failing": list(self.failing),
            "summary": self.summary,
            "conflicts": list(self.conflicts),
            "unfetchable": list(self.unfetchable),
        }


#: Signature of the injected "build this set, combined, and test it" function.
#: Every unit test passes a fake, so bisection is tested without git.
Tester = Callable[[Sequence[TrainPR]], TestResult]


@dataclass(frozen=True)
class PRVerdict:
    """What the train decided about one PR."""

    pr: int
    status: str
    reason: str = ""
    failing_tests: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "status": self.status,
            "reason": self.reason,
            "failing_tests": list(self.failing_tests),
        }


@dataclass
class TrainResult:
    """The whole run: what landed, what was set aside, and why.

    ``to_dict`` is the JSON report written per run.  ``landed`` and
    ``set_aside`` partition the batch, so every PR in the run carries exactly
    one verdict.
    """

    repo: str = ""
    ordered: tuple[TrainPR, ...] = ()
    verdicts: tuple[PRVerdict, ...] = ()
    test_runs: int = 0
    candidate_sha: str = ""
    detail: str = ""
    #: The branch the batch was folded onto, named in every verdict reason.
    base_branch: str = DEFAULT_BASE_BRANCH

    @property
    def landed(self) -> tuple[int, ...]:
        """PR numbers that merged, in merge order."""
        return tuple(v.pr for v in self.verdicts if v.status == LANDED)

    @property
    def set_aside(self) -> tuple[int, ...]:
        """PR numbers held back: conflict, regression, or a moved head."""
        return tuple(v.pr for v in self.verdicts if v.status != LANDED)

    def by_status(self, status: str) -> tuple[PRVerdict, ...]:
        """Every verdict carrying *status*, so a reader can ask one question."""
        return tuple(v for v in self.verdicts if v.status == status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": "merge.train",
            "repo": self.repo,
            "base_branch": self.base_branch,
            "batch_size": len(self.ordered),
            "ordered": [p.number for p in self.ordered],
            "landed": list(self.landed),
            "set_aside": list(self.set_aside),
            "landed_count": len(self.landed),
            "set_aside_count": len(self.set_aside),
            "test_runs": self.test_runs,
            "candidate_sha": self.candidate_sha,
            "detail": self.detail,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }

    def render_text(self) -> str:
        """A short operator-facing summary of the run."""
        if not self.ordered:
            return f"merge train [{self.repo}]: nothing to run"
        lines = [f"merge train [{self.repo}]: {len(self.ordered)} PR(s) in the batch"]
        for verdict in self.verdicts:
            marker = "OK" if verdict.status == LANDED else verdict.status
            reason = f"  {verdict.reason}" if verdict.reason else ""
            lines.append(f"  [{marker}] #{verdict.pr}{reason}")
        lines.append(
            f"  {len(self.landed)} landed, {len(self.set_aside)} set aside, "
            f"{self.test_runs} test run(s)"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure core: ordering, partitioning, bisection
# ---------------------------------------------------------------------------


def order_batch(prs: Sequence[TrainPR]) -> list[TrainPR]:
    """Order *prs* oldest first, honouring stacks.

    Stacks are the reason this is not simply ``sorted(by number)``: a PR whose
    ``base_ref`` names another PR's ``head_branch`` needs that PR merged first,
    or its base will not exist.  The map is built from head *branches* to PR
    numbers, never from ``base_ref`` to numbers — indexing a batch of PRs that
    all target ``main`` by their own base ref would make the last of them own
    ``main``, and then fold that PR ahead of any stack parent waiting on it.

    Each PR is placed as soon as the PRs it is stacked on have been emitted,
    which keeps the order as close to PR-number order as the stack constraints
    allow.

    Deterministic and total.  Deterministic because the result is a function of
    the *set* of PRs, not of the order they arrived in: the pending list is
    sorted by number before every pass, so a batch a caller happened to collect
    in a different order folds and merges identically.  Total because a cycle
    among base refs (a broken stack), or a base ref naming a PR outside the
    batch, cannot deadlock the order: a PR only ever waits on a PR not yet
    emitted, and the remainder is emitted in PR order.
    """
    pending = sorted(prs, key=lambda p: p.number)
    base_owner: dict[str, int] = {}
    for pr in pending:
        if pr.head_branch:
            base_owner.setdefault(pr.head_branch, pr.number)

    ordered: list[TrainPR] = []
    emitted: set[int] = set()
    while pending:
        progressed = False
        for pr in list(pending):
            base = base_owner.get(pr.base_ref) if pr.base_ref else None
            if base is not None and base != pr.number and base not in emitted:
                continue  # stacked on a PR that has not landed yet
            ordered.append(pr)
            emitted.add(pr.number)
            pending.remove(pr)
            progressed = True
        if not progressed:
            # Everything left waits on something also left: a cycle.  Emit the
            # rest in PR order rather than dropping approved work on the floor.
            ordered.extend(pending)
            break
    return ordered


def partition_batch(prs: Sequence[TrainPR]) -> tuple[list[TrainPR], list[TrainPR]]:
    """Split *prs* into the batch to test and the ones already disqualified.

    A PR whose head moved is dropped here rather than tested: a green result
    over commits the gate never reviewed would be a meaningless green light.
    """
    return [p for p in prs if not p.moved], [p for p in prs if p.moved]


def bisect(prs: Sequence[TrainPR], tester: Tester) -> tuple[list[TrainPR], list[PRVerdict], int]:
    """Find which members of a red batch are at fault.

    Returns ``(landable, culprits, runs)``: the PRs the search never showed to
    be at fault, which land; and one ``REGRESSION`` verdict per PR found to be
    at fault, tagged with the failing test ids.

    The split is the batch in half rather than a single-PR peel, because each
    test run is the expensive part: halving finds the fault in ``log2(n)`` runs
    where peeling one at a time needs up to ``n``.  Both halves are always
    tested — a red half says nothing about the other, so a half that was never
    run is never counted as cleared.
    """
    culprits: list[PRVerdict] = []
    runs = 0

    def test(batch: Sequence[TrainPR]) -> TestResult:
        nonlocal runs
        runs += 1
        return tester(batch)

    if len(prs) == 1:
        result = test(prs)
        if result.passed:
            return list(prs), [], runs
        return [], [_culprit(prs[0], result, "fails on its own against the candidate")], runs

    mid = len(prs) // 2
    left, right = list(prs[:mid]), list(prs[mid:])
    left_result, right_result = test(left), test(right)

    if left_result.passed and right_result.passed:
        # Each half is green but the whole batch was red: the fault needs the
        # combination, so no single PR is to blame.  Report the whole batch
        # rather than landing a combination already known to fail.  A green
        # half names no failing test, so the ids are left empty rather than
        # inherited from the red run.
        return [], [_culprit(p, None, "passes alone, fails in combination") for p in prs], runs

    landable: list[TrainPR] = []
    for half, result in ((left, left_result), (right, right_result)):
        if result.passed:
            landable.extend(half)
            continue
        survivors, found, half_runs = bisect(half, tester)
        runs += half_runs
        landable.extend(survivors)
        culprits.extend(found)
    return landable, culprits, runs


def _culprit(pr: TrainPR, result: TestResult | None, reason: str) -> PRVerdict:
    return PRVerdict(
        pr=pr.number,
        status=REGRESSION,
        reason=reason,
        failing_tests=result.failing if result else (),
    )


# ---------------------------------------------------------------------------
# The train
# ---------------------------------------------------------------------------


class Merger(Protocol):
    """Lands PRs on GitHub, in order.  Returns the PR numbers merged."""

    def land(self, prs: Sequence[TrainPR]) -> list[int]: ...


@dataclass
class MergeTrain:
    """The batch decision, over an injected build-and-test callable.

    Nothing here touches git, GitHub, or a clock, so the whole of the
    ordering / conflict-set-aside / bisection / bookkeeping logic is exercised
    by the unit tests against fakes.
    """

    tester: Tester
    repo: str = ""
    #: Branch the batch is folded onto, named in every verdict reason so a
    #: reader is never told a PR conflicted with a branch it was not folded on.
    base_branch: str = DEFAULT_BASE_BRANCH

    def run(self, prs: Sequence[TrainPR], *, merger: Merger | None = None) -> TrainResult:
        """Build, test, and land *prs* as one batch.

        The order of the run is the order of the risk: drop what is not
        eligible, fold the rest, test the combination once, and only then
        decide whether anything may be merged.  Nothing is merged from a red
        batch until the bisection has named what is safe.
        """
        result = TrainResult(repo=self.repo, base_branch=self.base_branch)
        keep, moved = partition_batch(prs)
        result.ordered = tuple(order_batch(keep))
        result.verdicts = tuple(_moved_verdicts(moved))
        if not result.ordered:
            result.detail = "no PR in the batch is eligible"
            return result

        result.test_runs = 1
        outcome = self.tester(result.ordered)
        batch = list(result.ordered)
        if outcome.unfetchable:
            # A head the checkout has no object for is neither a conflict nor a
            # test result, so it can neither be rebased nor bisected.  It is
            # still one PR among several, so it is set aside on its own and the
            # rest of the batch is tested rather than held back by a checkout
            # that needs a fetch.
            result.verdicts += tuple(
                _unfetchable_verdicts(outcome.unfetchable, batch, outcome.summary)
            )
            batch = [p for p in batch if p.number not in set(outcome.unfetchable)]
            if not batch:
                result.ordered = ()
                result.detail = (
                    f"the checkout has no object for {names_of(outcome.unfetchable)}; "
                    f"fetch the PR heads and run the train again"
                )
                return result
            result.test_runs += 1
            outcome = self.tester(batch)
        if outcome.conflicts:
            # A conflict is not a test failure: set those PRs aside and re-test
            # the fold that actually applied, so one conflicted PR costs one
            # extra run instead of dragging the whole batch into a bisect.
            result.verdicts += tuple(_conflict_verdicts(outcome.conflicts, batch, self.base_branch))
            batch = [p for p in batch if p.number not in set(outcome.conflicts)]
            result.test_runs += 1
            outcome = self.tester(batch) if batch else TestResult(passed=True)
        result.ordered = tuple(batch)
        if not batch:
            result.detail = f"every PR in the batch conflicted with origin/{self.base_branch}"
            return result

        if outcome.passed:
            result.verdicts += tuple(self._land(batch, merger, "combined tree passed"))
            result.detail = f"batch of {len(batch)} landed after one test run"
            return result

        landable: list[TrainPR] = []
        culprits: list[PRVerdict] = []
        if len(batch) == 1:
            # A lone PR that goes red has no other member to blame, so it is
            # named the way bisect() names the same situation.  Calling it
            # NEEDS-REBASE would tell an operator to rebase a branch that is
            # stale when the only thing wrong is the PR's own tests.
            culprits = [_culprit(batch[0], outcome, "fails on its own against the candidate")]
        else:
            landable, culprits, runs = bisect(batch, self.tester)
            result.test_runs += runs
        result.verdicts += tuple(self._land(landable, merger, "survived the red batch"))
        result.verdicts += tuple(culprits)
        names = ", ".join(f"#{c.pr}" for c in culprits)
        result.detail = f"combined tree failed; set aside {names} and landed the rest"
        return result

    def _land(self, prs: Sequence[TrainPR], merger: Merger | None, reason: str) -> list[PRVerdict]:
        """Merge *prs* as a unit and return one verdict per PR.

        The batch lands together and produces a single result, so the caller
        deploys once rather than once per PR.  A PR the merger declined — its
        head moved in the window between testing and merging — is recorded as
        moved, never as landed.
        """
        if not prs:
            return []
        if merger is None:
            return [PRVerdict(pr=p.number, status=LANDED, reason=reason) for p in prs]
        merged = set(merger.land(prs))
        return [
            PRVerdict(
                pr=p.number,
                status=LANDED if p.number in merged else SKIPPED_MOVED,
                reason=(
                    reason
                    if p.number in merged
                    else "head moved between the test run and the merge; not merged"
                ),
            )
            for p in prs
        ]


def _moved_verdicts(moved: Sequence[TrainPR]) -> list[PRVerdict]:
    return [
        PRVerdict(
            pr=pr.number,
            status=SKIPPED_MOVED,
            reason=(
                f"head moved to {pr.current_head[:9]} since the gate approved {pr.head_sha[:9]}"
            ),
        )
        for pr in sorted(moved, key=lambda p: p.number)
    ]


def names_of(numbers: Sequence[int]) -> str:
    """``#1, #2`` — the way a run's own text names a set of PRs."""
    return ", ".join(f"#{n}" for n in numbers)


def _unfetchable_verdicts(
    numbers: Sequence[int], batch: Sequence[TrainPR], detail: str = ""
) -> list[PRVerdict]:
    """Verdicts for heads the checkout has no commit for."""
    wanted = set(numbers)
    return [
        PRVerdict(
            pr=pr.number,
            status=UNFETCHABLE,
            reason=(
                f"head {pr.head_sha[:9]} could not be fetched, so the batch was never "
                f"built and this PR was not tested" + (f" ({detail})" if detail else "")
            ),
        )
        for pr in sorted(batch, key=lambda p: p.number)
        if pr.number in wanted
    ]


def _conflict_verdicts(
    conflicts: Sequence[int], batch: Sequence[TrainPR], base_branch: str
) -> list[PRVerdict]:
    return [
        PRVerdict(
            pr=pr.number,
            status=NEEDS_REBASE,
            reason=f"conflicts with the batch when merged onto origin/{base_branch}",
        )
        for pr in sorted(batch, key=lambda p: p.number)
        if pr.number in set(conflicts)
    ]


# ---------------------------------------------------------------------------
# git / gh runner
# ---------------------------------------------------------------------------


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run *argv*, never raising on a nonzero exit: every caller branches on it."""
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=timeout, cwd=str(cwd)
    )


def select_test_files(prs: Sequence[TrainPR], *, tree: Path) -> list[str]:
    """The test files covering *prs*, given the checkout *tree* they live in.

    A changed path that is itself a test is taken directly.  A changed module
    contributes its sibling test, under either common naming convention, when
    that file exists in *tree*.  Import-graph reachability is deliberately not
    walked: a ``--test-command`` covering the whole suite is the operator's
    lever for that, and guessing a wider set silently slows every bisect.
    """
    tests: set[str] = set()
    for pr in prs:
        for path in pr.files:
            if not path.endswith(".py"):
                continue
            module = Path(path)
            if module.name.startswith(_TEST_SUFFIXES):
                tests.add(path)
                continue
            for candidate in (f"test_{module.name}", f"{module.stem}_test.py"):
                if (tree / module.parent / candidate).is_file():
                    tests.add(str(module.parent / candidate))
    return sorted(tests)


def test_command_for(test_files: Sequence[str]) -> str:
    """The default test command: ``pytest`` over *test_files*.

    With no test files the command is a bare ``pytest``, which collects by the
    repository's own configuration rather than silently testing nothing.
    """
    files = " ".join(shlex.quote(f) for f in sorted(set(test_files)))
    return f"pytest {files}" if files else "pytest"


def scratch_root(repo: str) -> Path:
    """Where candidate worktrees live.

    Under the agent-fleet home rather than the system temp dir: /tmp is a
    tmpfs on this fleet's boxes, and a checked-out tree does not belong in RAM.
    """
    return agent_fleet_home() / "tmp" / "merge-train" / repo


def stack_roots(prs: Sequence[TrainPR]) -> list[TrainPR]:
    """The PRs in *prs* whose base is not another PR in the batch.

    A stacked PR's ``base_ref`` names a sibling's head branch, and that sibling
    is merged into the candidate before it — so naming the branch every member of
    a stack declares is naming a branch that only exists in the batch, and the
    fold has nothing to cut from.  The roots are the members that actually say
    which branch the batch merges into.

    Head *branches* key the map, for the same reason :func:`order_batch` uses
    them: a batch whose PRs all target ``main`` would otherwise hand ``main`` to
    the last of them and treat every other PR as stacked on it.  A member whose
    base names a PR outside the batch is a root too, because that branch is not
    going to appear in this run.
    """
    owner: dict[str, int] = {}
    for pr in prs:
        if pr.head_branch:
            owner.setdefault(pr.head_branch, pr.number)
    numbers = {pr.number for pr in prs}
    roots: list[TrainPR] = []
    for pr in prs:
        parent = owner.get(pr.base_ref)
        if parent is None or parent == pr.number or parent not in numbers:
            roots.append(pr)
    return roots


def resolve_base_branch(
    repo_path: Path,
    *,
    configured: str = "",
    prs: Sequence[TrainPR] = (),
) -> str:
    """The branch the batch is folded onto, most explicit answer first.

    An operator's ``--base-branch`` is an instruction and fleet.yaml's
    ``base_branch`` is a declaration, so both outrank anything read off the
    repository.  Past those, the base the batch itself declares wins: a PR the
    gate approved is a PR whose base ref is the branch it was opened against.
    Only the batch's *roots* are asked (:func:`stack_roots`), because a stacked
    PR's base names its parent in the same batch and is folded onto the candidate
    rather than onto any branch — asking the whole batch would read a perfectly
    good stack as a batch spanning two branches and refuse to run it.  Roots that
    still disagree have no single base, and saying so beats guessing one.

    Only then is the checkout asked, and only for its own answer —
    ``git ls-remote --symref origin HEAD`` for the remote's default branch,
    then the branch the checkout has checked out.  ``main`` is the last
    fallback, not the assumption: a repository that calls its default branch
    ``develop`` must never be folded onto a stale ``origin/main``.
    """
    if configured.strip():
        return configured.strip()
    bases = {pr.base_ref for pr in stack_roots(prs) if pr.base_ref.strip()}
    if len(bases) == 1:
        return bases.pop()
    if len(bases) > 1:
        raise ValueError(
            "the batch targets more than one base branch ("
            + ", ".join(sorted(bases))
            + "); pass --base-branch to say which one the train folds onto"
        )
    return _default_branch(repo_path) or DEFAULT_BASE_BRANCH


def _default_branch(repo_path: Path) -> str:
    """The branch this repository folds onto, from the remote or the checkout."""
    head = _run(["git", "ls-remote", "--symref", "origin", "HEAD"], cwd=repo_path, timeout=60)
    if head.returncode == 0:
        first = head.stdout.splitlines()[0] if head.stdout.splitlines() else ""
        if first.startswith("ref:"):
            return first.split()[1].removeprefix("refs/heads/").strip()
    current = _run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo_path,
        timeout=60,
    )
    if current.returncode == 0:
        return current.stdout.strip()
    return ""


def ensure_heads_local(repo_path: Path, prs: Sequence[TrainPR]) -> tuple[list[TrainPR], str]:
    """Make sure the checkout holds every head in *prs*.

    A checkout cloned before the PRs opened has no object for any of their
    heads: ``git fetch origin <base>`` updates one ref and nothing else.
    Handing a missing object to ``git merge`` is not a conflict — it fails with
    "not something we can merge", and reading that as a conflict sets the whole
    batch aside as stale and lands nothing.

    Each missing head is fetched twice over, as
    :func:`~agent_fleet.merge_plan.batching.ensure_commits_local` already does
    for the merge check: by ``refs/pull/<n>/head``, which GitHub always serves
    even for a fork, and by raw SHA, for a remote that advertises
    ``allowReachableSHA1InWant``.  A head that cannot be materialised at all is
    reported rather than folded, so a broken checkout is named instead of
    mistaken for a branch that needs rebasing.

    The returned pair says which heads the checkout holds *and* which it could
    not be made to hold, so a caller can never fold a partial batch without also
    reporting the PRs it dropped: every head in *prs* is in exactly one of the
    two.  This mutates ``.git``, so it is called from inside the fold's lock —
    see :meth:`GitFold.ensure_heads_local`.
    """
    missing = [pr for pr in prs if pr.head_sha and not _commit_exists(repo_path, pr.head_sha)]
    if not missing:
        return list(prs), ""
    reasons: list[str] = []
    for pr in missing:
        reason = _fetch_head(repo_path, pr)
        if not reason:
            continue
        reasons.append(f"#{pr.number} {pr.head_sha[:9]}: {reason}")
    return [pr for pr in prs if _commit_exists(repo_path, pr.head_sha)], "; ".join(reasons)


def _commit_exists(repo_path: Path, sha: str) -> bool:
    result = _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_path, timeout=60)
    return result.returncode == 0


def _fetch_head(repo_path: Path, pr: TrainPR) -> str:
    """Fetch one PR head; returns ``""`` when it worked, else the reason.

    The pull ref is tried first and the raw SHA second, and **one refspec per
    fetch, never two in one command**: ``git fetch`` gives up on the whole
    invocation when it cannot resolve any of them, so a
    ``refs/pull/<n>/head`` the remote does not serve — which is every PR on a
    remote that is not a pull-request mirror — would take the SHA fetch down
    with it and leave the head unfetchable for a reason that has nothing to do
    with the head.
    """
    refspecs = [pr.head_sha]
    if pr.head_ref or pr.number:
        refspecs.insert(0, f"+refs/pull/{pr.number}/head")
    reason = ""
    for refspec in refspecs:
        result = _run(["git", "fetch", "--quiet", "origin", refspec], cwd=repo_path, timeout=600)
        if result.returncode == 0:
            return ""
        reason = (result.stderr or result.stdout).strip()
        logger.info("train: fetch %s failed: %s", refspec, reason)
    return reason.splitlines()[-1] if reason else "the remote has no object for it"


def _merge_is_conflict(result: subprocess.CompletedProcess[str], worktree: Path) -> bool:
    """Whether a failed ``git merge`` in *worktree* failed on content, not on a ref.

    Only a tree conflict leaves unmerged index entries behind, so those are the
    discriminator.  Deciding it any other way reads "not something we can merge"
    — a head the checkout never fetched — as a conflict and sends the PR off
    to be rebased against a base it was never merged onto.  The entries are read
    through ``git`` rather than off ``.git/MERGE_HEAD``, which in a *linked*
    worktree is a file, not the directory the candidate lives in.
    """
    if result.returncode == 0:
        return False
    unmerged = _run(["git", "ls-files", "--unmerged"], cwd=worktree, timeout=60)
    if unmerged.returncode != 0:
        return "CONFLICT (content)" in (result.stdout or result.stderr or "")
    return unmerged.stdout.strip() != ""


#: A missing object.  ``git merge`` reports both a head the checkout has no
#: commit for and one it cannot reconcile ("refusing to merge unrelated
#: histories") with the same sentence, and the second one is still a real,
#: resolvable tree conflict the operator or the train has to answer for.
_MERGE_UNRESOLVABLE = (
    "not something we can merge",
    "unrelated histories",
    " refusing to merge unrelated",
    "cannot merge",
)


def _merge_is_unfetchable(result: subprocess.CompletedProcess[str], worktree: Path) -> bool:
    """Whether a failed merge failed because the checkout has no such commit.

    A merge that leaves the tree alone for any other reason — no committer
    identity, a hook that refused, a full disk — is neither a conflict nor a
    missing head, and calling it a missing head would set aside PRs that are
    perfectly good and tell an operator to fetch something that is already
    there.  Those failures belong to the run, not to a verdict about a PR.
    """
    if result.returncode == 0 or _merge_is_conflict(result, worktree):
        return False
    message = f"{result.stdout or ''}{result.stderr or ''}"
    return any(token in message for token in _MERGE_UNRESOLVABLE)


class GitFold:
    """Folds PR heads onto the repository's base branch in a throwaway worktree.

    ``git merge --no-ff`` per PR; a PR that conflicts is set aside and the fold
    continues onto the clean tree, so one conflicted PR cannot block the rest
    of the batch.  The base branch is the one :func:`resolve_base_branch` chose,
    never a literal ``main``.  The worktree is created per run under the
    repository's worktree lock — a fold mutates the shared ``.git`` directory,
    and an unlocked ``worktree add`` can lose a sibling's half-registered
    worktree to a concurrent prune.

    Entering fetches the base and *refuses to continue if that fetch failed*.
    The candidate is cut from ``origin/<base>``, so a checkout whose fetch
    failed folds and tests the batch against whatever commits ``origin/<base>``
    happened to hold beforehand, and the run then lands a combination that was
    never tested against the base it is merged into.  There is no honest verdict
    for that, so it stops the run instead of reporting one.
    """

    def __init__(self, repo_path: Path, base_branch: str = DEFAULT_BASE_BRANCH) -> None:
        self.repo_path = Path(repo_path)
        self.base_branch = base_branch
        self._worktree: Path | None = None
        self.conflicted: list[int] = []
        self.unfetchable: list[int] = []
        self.candidate_sha = ""
        #: PRs the checkout could not be made to hold, and why.  The fetch that
        #: would fetch them runs inside ``__enter__``, under the same lock as
        #: the worktree add and the merges — see :meth:`ensure_heads_local`.
        self.missing: list[int] = []
        self.missing_reason = ""

    @property
    def base_ref(self) -> str:
        """The fetched ref the candidate is cut from."""
        return f"origin/{self.base_branch}"

    def __enter__(self) -> GitFold:
        fetch = self._git(["git", "fetch", "--quiet", "origin", self.base_branch])
        if fetch.returncode != 0:
            msg = (
                f"could not fetch origin/{self.base_branch} to fold onto: "
                f"{(fetch.stderr or fetch.stdout).strip()}"
            )
            raise RuntimeError(msg)
        root = scratch_root(self.repo_path.name) / f"wt-{id(self):x}"
        root.mkdir(parents=True, exist_ok=True)
        add = self._git(
            ["git", "worktree", "add", "--detach", str(root), self.base_ref],
            cwd=self.repo_path,
        )
        if add.returncode != 0:
            shutil.rmtree(root, ignore_errors=True)
            msg = (
                f"could not create a candidate worktree from {self.base_ref}: {add.stderr.strip()}"
            )
            raise RuntimeError(msg)
        self._worktree = root
        return self

    def ensure_heads_local(self, prs: Sequence[TrainPR]) -> list[TrainPR]:
        """Fetch the heads *prs* needs, and return the ones the checkout holds.

        Called from inside the fold, under the lock :class:`GitTrainer` already
        holds, because a fetch is a mutation of the shared ``.git`` exactly like
        the ``worktree add`` beside it.  Run outside the lock it reintroduces the
        race the lock exists to close: a sibling train can move
        ``refs/pull/<n>/head`` between this process's ``cat-file -e`` and its
        ``git merge``, and the merge dereferences the ref to the newer commits —
        so the candidate advances by code the gate never approved and no test run
        ever saw.  A head's identity is settled by a merged merge commit that
        records the SHA, so a bare SHA cannot be swapped underneath it, and
        moving a ref out from under a locked fold is left to the other train to
        refuse rather than to be detected after the fact.
        """
        foldable, reason = ensure_heads_local(self.repo_path, prs)
        self.missing = [pr.number for pr in prs if pr.number not in {p.number for p in foldable}]
        self.missing_reason = reason
        return foldable

    def __exit__(self, *exc: object) -> None:
        if self._worktree is not None:
            self._git(["git", "worktree", "remove", "--force", str(self._worktree)])
            shutil.rmtree(self._worktree, ignore_errors=True)
            self._worktree = None

    @property
    def worktree(self) -> Path:
        """The candidate checkout; only readable inside the context manager."""
        if self._worktree is None:
            msg = "the candidate worktree is not open"
            raise RuntimeError(msg)
        return self._worktree

    def fold(self, prs: Sequence[TrainPR]) -> str:
        """Merge each of *prs* in turn; returns the resulting head SHA.

        Three outcomes, kept apart on purpose.  A head that is still missing
        after the fetch is ``unfetchable`` and never merged: the candidate is
        left at the base rather than advanced by a commit that is not the PR's.
        A tree conflict is ``conflicted``, and the merge is aborted so the next
        PR folds onto a clean tree.  Anything else — an unset committer
        identity, a refusing hook — is a fault in the *checkout*, not a
        verdict about a PR, so it stops the run instead of quietly setting good
        work aside under a reason that is not true.

        Every git call in the loop names ``self.worktree`` as its directory.
        That is not tidiness: ``git merge --abort`` and ``git rev-parse HEAD``
        default to the operator's checkout, where there is no merge to abort and
        whose ``HEAD`` is whatever commit the operator happened to be on.  An
        abort left in the candidate instead strands its unmerged entries, and
        every PR after the conflict is then set aside for a conflict that is not
        its own; a head read from the wrong directory reports the operator's
        commit as the tree that was tested.
        """
        self.conflicted = []
        self.unfetchable = []
        for pr in prs:
            if not _commit_exists(self.repo_path, pr.head_sha):
                self.unfetchable.append(pr.number)
                continue
            merged = self._merge(pr.head_sha)
            if merged.returncode == 0:
                continue
            if _merge_is_conflict(merged, self.worktree):
                self._git(["git", "merge", "--abort"], cwd=self.worktree)
                self.conflicted.append(pr.number)
                continue
            if _merge_is_unfetchable(merged, self.worktree):
                self.unfetchable.append(pr.number)
                continue
            msg = (
                f"could not fold #{pr.number} ({pr.head_sha[:9]}) onto "
                f"{self.base_ref}: {(merged.stderr or merged.stdout).strip()}"
            )
            raise RuntimeError(msg)
        self.candidate_sha = self._git(
            ["git", "rev-parse", "HEAD"], cwd=self.worktree
        ).stdout.strip()
        return self.candidate_sha

    def _merge(self, sha: str) -> subprocess.CompletedProcess[str]:
        """``git merge`` in the candidate, with *sha* named in its error text."""
        merged = _run(
            ["git", "merge", "--no-ff", "--no-edit", sha],
            cwd=self.worktree,
            timeout=600,
        )
        if merged.returncode != 0 and "not something we can merge" in merged.stderr:
            merged.stderr += f" (head {sha} is not a commit this checkout holds)\n"
        return merged

    def _git(
        self, argv: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return _run(argv, cwd=cwd or self.repo_path)


class GitTrainer:
    """The real ``evaluate``: fold a set of PRs, then test the combined tree.

    Each call folds exactly the PRs it was handed, so a bisect half is tested
    against its own combination and not against a tree still carrying the
    batch's other half.  A test command that outlives ``timeout`` is killed and
    reported red, so a hang is a test outcome the train can bisect rather than
    an exception that abandons the run mid-fold.

    One lock covers the whole call, from fetching the heads to the last merge.
    The head fetch mutates the shared ``.git`` exactly as the worktree add does,
    so a fold whose fetch ran outside the lock would leave the window between
    ``cat-file -e <head>`` and ``git merge <head>`` open to a sibling run moving
    the PR's ref — and the fold would then advance by commits no test ever saw.
    """

    def __init__(
        self,
        repo_path: Path,
        *,
        command: str | None = None,
        timeout: int = DEFAULT_TEST_TIMEOUT,
        base_branch: str = "",
    ) -> None:
        self.repo_path = Path(repo_path)
        self.command = command
        self.timeout = timeout
        self.base_branch = base_branch or DEFAULT_BASE_BRANCH
        self.candidate_sha = ""

    def evaluate(self, prs: Sequence[TrainPR]) -> TestResult:
        """Fold *prs* onto the base branch and run the test command over the result."""
        if not prs:
            return TestResult(passed=True, summary="empty batch")
        with repo_worktree_lock(self.repo_path), GitFold(self.repo_path, self.base_branch) as fold:
            foldable = fold.ensure_heads_local(prs)
            unfetchable = _fold_incomplete(
                fold,
                "no head in the batch could be fetched" if not foldable else "",
            )
            if unfetchable is not None:
                return unfetchable
            fold.fold(foldable)
            self.candidate_sha = fold.candidate_sha
            incomplete = _fold_incomplete(
                fold, f"fold incomplete before the test run: {fold.missing_reason}"
            )
            if incomplete is not None:
                return incomplete
            argv = self._argv(foldable, fold)
            try:
                result = _run(argv, cwd=fold.worktree, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                return TestResult(
                    passed=False,
                    summary=(
                        f"test command exceeded its {self.timeout}s timeout after folding "
                        f"{len(foldable)} PR(s) and was killed"
                    ),
                )
        output = (result.stdout or result.stderr or "").strip()
        return TestResult(
            passed=result.returncode == 0,
            failing=_failing_tests(output),
            summary=output[-4000:],
        )

    def _argv(self, prs: Sequence[TrainPR], fold: GitFold) -> list[str]:
        if self.command:
            argv = shlex.split(self.command)
        else:
            argv = shlex.split(test_command_for(select_test_files(prs, tree=fold.worktree)))
        # A configured command may want to know where the tree is; {tree} keeps
        # it from hardcoding a path it cannot know.
        return [a.replace("{tree}", str(fold.worktree)) for a in argv]


def _fold_incomplete(fold: GitFold, prefix: str) -> TestResult | None:
    """A fold that applied only part of the batch, or ``None`` when it applied all.

    Every way the fold can come up short is folded into one verdict here, because
    they are the same thing to the caller: a PR in the batch was not tested.  A
    head the checkout never got (:attr:`GitFold.missing`, the fetch that could not
    materialise it) counts exactly like a head that went missing between the fetch
    and the merge (:attr:`GitFold.unfetchable`) and like one the merge could not
    resolve.

    Collapsing them is also what keeps the report honest.  Reporting only the
    last group means the PRs the fetch dropped are in neither ``landed`` nor
    ``set_aside``: an approved PR the run simply never looked at, with no verdict
    and nothing in the text an operator reads.  The whole test run is built from
    the fold's state this way rather than from whichever field happens to be
    populated, so no path can report a partial batch as a whole one.
    """
    unfetchable = fold.missing + [n for n in fold.unfetchable if n not in fold.missing]
    if not unfetchable and not fold.conflicted:
        return None
    parts = [
        text
        for text in (
            f"unfetchable {names_of(unfetchable)}" if unfetchable else "",
            f"conflicting {names_of(fold.conflicted)}" if fold.conflicted else "",
        )
        if text
    ]
    detail = "; ".join(parts)
    summary = f"{prefix}: {detail}" if prefix else detail
    return TestResult(
        passed=False,
        summary=summary,
        conflicts=tuple(fold.conflicted),
        unfetchable=tuple(unfetchable),
    )


def _failing_tests(output: str) -> tuple[str, ...]:
    """Pull failing test ids out of a pytest-style output.

    Deliberately conservative: an unrecognised red run reports no ids rather
    than a guessed set, so a culprit report never names a test that passed.
    """
    found: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("FAILED ", "ERROR ")):
            continue
        token = stripped.split(" ")[1] if " " in stripped else stripped
        if token and token not in found:
            found.append(token)
    return tuple(found)


class GitMerger:
    """Lands PRs with ``gh pr merge --merge``, re-verifying each head first.

    The head is re-read immediately before the merge: the batch was tested
    against these commits, so a PR that has moved since the fold is not merged.
    Hooks keep running — no ``--no-verify`` and no force.
    """

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path)

    def land(self, prs: Sequence[TrainPR]) -> list[int]:
        landed: list[int] = []
        for pr in prs:
            if not self._head_unchanged(pr):
                continue
            merged = _run(["gh", "pr", "merge", str(pr.number), "--merge"], cwd=self.repo_path)
            if merged.returncode == 0:
                landed.append(pr.number)
            else:
                logger.info("train: gh pr merge #%s failed: %s", pr.number, merged.stderr.strip())
        return landed

    def _head_unchanged(self, pr: TrainPR) -> bool:
        view = _run(
            ["gh", "pr", "view", str(pr.number), "--json", "headRefOid,state"],
            cwd=self.repo_path,
        )
        if view.returncode != 0:
            logger.info("train: cannot re-read #%s, not merging", pr.number)
            return False
        try:
            detail = json.loads(view.stdout)
        except json.JSONDecodeError:
            return False
        if detail.get("state") != "OPEN":
            return False
        # Prefix comparison for the same reason as ``TrainPR.moved``: the
        # approved SHA is short, GitHub's headRefOid is full.
        head = str(detail.get("headRefOid") or "")
        return bool(head) and head.startswith(pr.head_sha)


def run_train(
    *,
    repo: str,
    repo_path: Path,
    prs: Sequence[TrainPR],
    command: str | None = None,
    max_batch_size: int = 5,
    report_path: Path | None = None,
    base_branch: str = "",
) -> TrainResult:
    """Run one train for *repo*, write its JSON report, and return the result.

    The git/gh entry point the CLI calls.  Ordering and staleness are applied
    here so the cap applies to what will actually be merged rather than to
    approvals that are about to be dropped — and so is the base branch, resolved
    once before the fold and carried into the trainer, the run, and the report so
    every verdict names the branch the batch was actually folded onto.

    The CLI resolves the base branch itself and hands it in, because it has to
    ask about the same capped batch it is about to run: two passes over two sets
    of PRs is how the branch the batch was folded onto stops being the branch the
    PRs merge into.  A caller that passes ``base_branch=""`` gets the same
    resolution over its own capped batch, and an empty *prs* is a no-op run
    rather than a fold of nothing.
    """
    keep, _moved = partition_batch(prs)
    batch = order_batch(keep)[:max_batch_size]
    base = resolve_base_branch(repo_path, configured=base_branch, prs=batch)
    trainer = GitTrainer(repo_path, command=command, base_branch=base)
    result = MergeTrain(tester=trainer.evaluate, repo=repo, base_branch=base).run(
        batch, merger=GitMerger(repo_path)
    )
    result.candidate_sha = trainer.candidate_sha
    path = Path(report_path) if report_path else scratch_root(repo) / "train-report.json"
    payload = result.to_dict()
    payload["command"] = command or test_command_for(())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning("train: could not write report %s: %s", path, exc)
    else:
        result.detail = f"{result.detail} (report: {path})".strip()
    return result
