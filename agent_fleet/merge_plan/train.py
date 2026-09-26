"""Land approved PRs in batches, testing the *combination* once.

With ~100 open agent PRs against one moving main, the expensive path is
merge -> conflict -> rebase -> re-gate, repeated one PR at a time.  Most of
today's gate failures are "fails once main is merged in": a conflict, or a
regression that only shows up alongside the other work.  The fix is to answer
that question once for the whole batch instead of once per PR.

One run, for one repository
---------------------------
1. **Order** the approved PRs oldest first, respecting stacks: a PR whose base
   is another PR's branch lands after that PR (:func:`order_batch`).
2. **Fold** each PR head into a candidate branch built from ``origin/main``
   with ``git merge --no-ff``, in a throwaway worktree so the operator's
   checkout is never touched.  A PR that conflicts is set aside as
   ``NEEDS-REBASE`` and the fold continues without it — one conflicted PR must
   not hold back work that has nothing to do with it.
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

    ``head_sha`` is the approved head the train merges.  ``base_ref`` names the
    PR's base branch, which is what makes stack order recoverable: a PR based
    on another PR's branch has to land after it or its base will not exist.
    ``current_head`` is where the head points at *now*; when it disagrees with
    ``head_sha`` the gate never reviewed those commits and the PR is dropped.
    """

    number: int
    head_sha: str
    base_ref: str = ""
    current_head: str = ""
    title: str = ""
    files: tuple[str, ...] = ()

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
    a merge conflict.
    """

    passed: bool
    failing: tuple[str, ...] = ()
    summary: str = ""
    conflicts: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failing": list(self.failing),
            "summary": self.summary,
            "conflicts": list(self.conflicts),
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
    ``base_ref`` is another PR's head branch needs that PR merged first, or it
    has nothing to merge into.  Each PR is placed as soon as the PRs it is
    stacked on have been emitted, which keeps the order as close to PR-number
    order as the stack constraints allow.

    Deterministic and total: a cycle among base refs (a broken stack), or a base
    ref naming a PR outside the batch, cannot deadlock the order, because a PR
    only ever waits on a PR not yet emitted and the remainder is emitted in PR
    order.
    """
    ordered: list[TrainPR] = []
    emitted: set[int] = set()
    pending = sorted(prs, key=lambda p: p.number)
    base_owner = {p.base_ref: p.number for p in prs if p.base_ref}
    while pending:
        progressed = False
        for pr in list(pending):
            base = base_owner.get(pr.base_ref)
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

    def run(self, prs: Sequence[TrainPR], *, merger: Merger | None = None) -> TrainResult:
        """Build, test, and land *prs* as one batch.

        The order of the run is the order of the risk: drop what is not
        eligible, fold the rest, test the combination once, and only then
        decide whether anything may be merged.  Nothing is merged from a red
        batch until the bisection has named what is safe.
        """
        result = TrainResult(repo=self.repo)
        keep, moved = partition_batch(prs)
        result.ordered = tuple(order_batch(keep))
        result.verdicts = tuple(_moved_verdicts(moved))
        if not result.ordered:
            result.detail = "no PR in the batch is eligible"
            return result

        result.test_runs = 1
        outcome = self.tester(result.ordered)
        batch = list(result.ordered)
        if outcome.conflicts:
            # A conflict is not a test failure: set those PRs aside and re-test
            # the fold that actually applied, so one conflicted PR costs one
            # extra run instead of dragging the whole batch into a bisect.
            result.verdicts += tuple(_conflict_verdicts(outcome.conflicts, batch))
            batch = [p for p in batch if p.number not in set(outcome.conflicts)]
            result.test_runs += 1
            outcome = self.tester(batch) if batch else TestResult(passed=True)
        result.ordered = tuple(batch)
        if not batch:
            result.detail = "every PR in the batch conflicted with origin/main"
            return result

        if outcome.passed:
            result.verdicts += tuple(self._land(batch, merger, "combined tree passed"))
            result.detail = f"batch of {len(batch)} landed after one test run"
            return result

        if len(batch) == 1:
            landable, culprits = (
                [],
                [
                    PRVerdict(
                        pr=batch[0].number,
                        status=NEEDS_REBASE,
                        reason="fails the combined-tree test against origin/main",
                        failing_tests=outcome.failing,
                    )
                ],
            )
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


def _conflict_verdicts(conflicts: Sequence[int], batch: Sequence[TrainPR]) -> list[PRVerdict]:
    return [
        PRVerdict(
            pr=pr.number,
            status=NEEDS_REBASE,
            reason="conflicts with the batch when merged onto origin/main",
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


class GitFold:
    """Folds PR heads onto ``origin/main`` in a throwaway worktree.

    ``git merge --no-ff`` per PR; a PR that conflicts is set aside and the fold
    continues onto the clean tree, so one conflicted PR cannot block the rest
    of the batch.  The worktree is created per run under the repository's
    worktree lock — a fold mutates the shared ``.git`` directory, and an
    unlocked ``worktree add`` can lose a sibling's half-registered worktree to
    a concurrent prune.
    """

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path)
        self._worktree: Path | None = None
        self.conflicted: list[int] = []
        self.candidate_sha = ""

    def __enter__(self) -> GitFold:
        self._git(["git", "fetch", "--quiet", "origin", "main"])
        root = scratch_root(self.repo_path.name) / f"wt-{id(self):x}"
        root.mkdir(parents=True, exist_ok=True)
        add = self._git(
            ["git", "worktree", "add", "--detach", str(root), "origin/main"],
            cwd=self.repo_path,
        )
        if add.returncode != 0:
            shutil.rmtree(root, ignore_errors=True)
            msg = f"could not create a candidate worktree: {add.stderr.strip()}"
            raise RuntimeError(msg)
        self._worktree = root
        return self

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
        """Merge each of *prs* in turn; returns the resulting head SHA."""
        self.conflicted = []
        for pr in prs:
            merged = self._git(["git", "merge", "--no-ff", "--no-edit", pr.head_sha])
            if merged.returncode == 0:
                continue
            # A conflict leaves the index dirty; abort so the next PR folds onto
            # a clean tree rather than onto the wreckage.
            self._git(["git", "merge", "--abort"])
            self.conflicted.append(pr.number)
        self.candidate_sha = self._git(["git", "rev-parse", "HEAD"]).stdout.strip()
        return self.candidate_sha

    def _git(
        self, argv: Sequence[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return _run(argv, cwd=cwd or self.repo_path)


class GitTrainer:
    """The real ``evaluate``: fold a set of PRs, then test the combined tree.

    Each call folds exactly the PRs it was handed, so a bisect half is tested
    against its own combination and not against a tree still carrying the
    batch's other half.
    """

    def __init__(
        self,
        repo_path: Path,
        *,
        command: str | None = None,
        timeout: int = DEFAULT_TEST_TIMEOUT,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.command = command
        self.timeout = timeout
        self.candidate_sha = ""

    def evaluate(self, prs: Sequence[TrainPR]) -> TestResult:
        """Fold *prs* onto origin/main and run the test command over the result."""
        if not prs:
            return TestResult(passed=True, summary="empty batch")
        with repo_worktree_lock(self.repo_path), GitFold(self.repo_path) as fold:
            fold.fold(prs)
            self.candidate_sha = fold.candidate_sha
            if fold.conflicted:
                return TestResult(
                    passed=False,
                    summary="conflicting PRs set aside before the test run",
                    conflicts=tuple(fold.conflicted),
                )
            argv = self._argv(prs, fold)
            result = _run(argv, cwd=fold.worktree, timeout=self.timeout)
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
) -> TrainResult:
    """Run one train for *repo*, write its JSON report, and return the result.

    The git/gh entry point the CLI calls.  Ordering and staleness are applied
    here so the cap applies to what will actually be merged rather than to
    approvals that are about to be dropped.
    """
    keep, _moved = partition_batch(prs)
    batch = order_batch(keep)[:max_batch_size]
    trainer = GitTrainer(repo_path, command=command)
    result = MergeTrain(tester=trainer.evaluate, repo=repo).run(batch, merger=GitMerger(repo_path))
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
