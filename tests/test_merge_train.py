"""Tests for the merge train: land approved PRs as one tested batch.

The pure core (ordering, staleness, conflict set-aside, bisection,
bookkeeping) is tested against an injected fake tester, so none of it needs
git or a network.  One integration test drives the real git fold over a temp
repository with a local ``origin``, because the fold is the part a fake cannot
honestly stand in for.

Covers:
- stacks order after their base PR, and a broken stack cannot deadlock
- a PR whose head moved is dropped before the batch is built
- a green batch is tested once and lands whole
- a conflict sets only that PR aside and the rest of the batch still lands
- a red batch is bisected: the culprit is named with its failing tests, the
  rest still lands, and the survivors are exactly the PRs proven good
"""

from __future__ import annotations

import argparse
import itertools
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

# ``TestResult`` starts with "Test" and ``test_command_for`` starts with
# "test", so pytest collects both and tries to resolve their parameters as
# fixtures.  They are imported under underscore aliases instead; the library
# should not have to know it is sometimes under pytest.
from agent_fleet.merge_plan.collect import GitHubClient
from agent_fleet.merge_plan.train import (
    LANDED,
    NEEDS_REBASE,
    REGRESSION,
    SKIPPED_MOVED,
    GitTrainer,
    MergeTrain,
    TrainPR,
    bisect,
    order_batch,
    partition_batch,
    run_train,
    select_test_files,
)
from agent_fleet.merge_plan.train import TestResult as _TestResult
from agent_fleet.merge_plan.train import test_command_for as _test_command_for

if TYPE_CHECKING:
    from collections.abc import Sequence


def pr(number: int, *, base: str = "", head: str = "", files: tuple[str, ...] = ()) -> TrainPR:
    return TrainPR(
        number=number,
        head_sha=f"sha{number}",
        base_ref=base,
        head_branch=head or f"feat/pr-{number}",
        files=files,
    )


# ---------------------------------------------------------------------------
# Fakes: no git, no network
# ---------------------------------------------------------------------------


@dataclass
class FakeTester:
    """Stands in for "fold this set and test it", keyed by PR number.

    ``bad`` is the set of PR numbers that fail on their own, ``together`` the
    set that only fails in combination — the two ways a batch goes red.  Every
    set of PRs it is asked about is recorded, so a test can assert on *how many*
    times the tree was tested, which is the whole point of the train.
    """

    bad: frozenset[int] = frozenset()
    together: frozenset[int] = frozenset()
    conflicts: frozenset[int] = frozenset()
    called: list[tuple[int, ...]] = field(default_factory=list)
    failing: tuple[str, ...] = ("tests/test_x.py::test_boom",)

    def __call__(self, batch: Sequence[TrainPR]) -> _TestResult:
        numbers = tuple(sorted(p.number for p in batch))
        self.called.append(numbers)
        hit = self.conflicts & set(numbers)
        if hit:
            return _TestResult(passed=False, conflicts=tuple(sorted(hit)))
        if self.bad & set(numbers):
            return _TestResult(passed=False, failing=self.failing)
        if self.together and self.together.issubset(set(numbers)):
            return _TestResult(passed=False, failing=self.failing)
        return _TestResult(passed=True)


@dataclass
class FakeMerger:
    """Records the batches handed to GitHub; merges nothing."""

    landed: list[int] = field(default_factory=list)
    decline: frozenset[int] = frozenset()

    def land(self, prs: Sequence[TrainPR]) -> list[int]:
        merged = [p.number for p in prs if p.number not in self.decline]
        self.landed.extend(merged)
        return merged


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_order_is_oldest_first_without_stacks() -> None:
    assert [p.number for p in order_batch([pr(3), pr(1), pr(2)])] == [1, 2, 3]


def test_a_stacked_pr_follows_its_base_even_at_a_lower_number() -> None:
    # #7 is based on branch of #4, so it must not be folded before #4.
    ordered = order_batch([pr(7, base="feat/pr-4"), pr(4, base="main"), pr(5)])
    assert [p.number for p in ordered] == [4, 5, 7]


def test_a_stacked_pr_is_ordered_by_head_branch_not_by_its_base_ref() -> None:
    # #3 sits on #1's branch.  Two other PRs also target main, so a map keyed
    # on base_ref would hand "main" to whichever came last and then block every
    # other PR on it — folding the stack child before its own parent.
    ordered = order_batch([pr(1, base="main"), pr(2, base="main"), pr(3, base="feat/pr-1")])
    assert [p.number for p in ordered] == [1, 2, 3]


def test_a_deep_stack_survives_unrelated_prs_targeting_the_same_base() -> None:
    prs = [pr(1, base="main"), pr(2, base="feat/pr-1"), pr(3, base="main"), pr(4, base="feat/pr-2")]
    assert [p.number for p in order_batch(prs)] == [1, 2, 3, 4]


def test_a_pr_with_no_known_head_branch_is_ordered_as_ordinary_work() -> None:
    # GitHub did not report a head branch: the PR is still approved work and
    # must land in number order rather than be dropped.
    unlabelled = TrainPR(number=2, head_sha="b", base_ref="main")
    assert [p.number for p in order_batch([pr(1), unlabelled])] == [1, 2]


def test_a_three_deep_stack_keeps_its_order() -> None:
    prs = [pr(1), pr(2, base="feat/pr-1"), pr(3, base="feat/pr-2")]
    assert [p.number for p in order_batch(prs)] == [1, 2, 3]


def test_a_circular_stack_still_orders_instead_of_hanging() -> None:
    ordered = order_batch([pr(1, base="feat/pr-2"), pr(2, base="feat/pr-1")])
    assert sorted(p.number for p in ordered) == [1, 2]


def test_a_base_ref_outside_the_batch_does_not_block_it() -> None:
    assert [p.number for p in order_batch([pr(1, base="feat/not-in-batch")])] == [1]


def test_order_is_deterministic_for_identical_input() -> None:
    prs = [pr(5), pr(2, base="feat/pr-1"), pr(4, base="feat/pr-3"), pr(1)]
    assert [p.number for p in order_batch(prs)] == [
        p.number for p in order_batch(list(reversed(prs)))
    ]


def test_order_does_not_depend_on_the_order_the_batch_was_collected_in() -> None:
    # The fold and merge sequence is driven by this order, so a batch collected
    # in a different order must not ship a different tree.
    prs = [pr(n, base="main") for n in range(1, 7)]
    expected = [1, 2, 3, 4, 5, 6]
    for permutation in itertools.permutations(prs):
        assert [p.number for p in order_batch(permutation)] == expected


def test_a_stacked_batch_folds_identically_however_it_was_collected() -> None:
    # A map keyed on base_ref hands "main" to #4 here, so #1 and #2 both end up
    # blocked on it and the stack child #3 lands before its own parent.
    prs = [pr(1, base="main"), pr(2, base="main"), pr(3, base="feat/pr-1"), pr(4, base="main")]
    expected = [1, 2, 3, 4]
    for permutation in itertools.permutations(prs):
        assert [p.number for p in order_batch(permutation)] == expected


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_partition_splits_a_moved_head_out_of_the_batch() -> None:
    moved = TrainPR(number=2, head_sha="aaa111", current_head="bbb222")
    keep, dropped = partition_batch([pr(1), moved])
    assert [p.number for p in keep] == [1]
    assert [p.number for p in dropped] == [2]


def test_a_short_approved_sha_is_not_read_as_a_moved_head() -> None:
    # A gate approval line records a short SHA; GitHub reports the full head.
    # Comparing them for equality would drop every healthy PR in the batch.
    shortened = TrainPR(number=1, head_sha="dc91fac", current_head="dc91fac7c9233ec3da9e9")
    assert shortened.moved is False
    assert partition_batch([shortened])[0] == [shortened]


def test_a_moved_head_is_never_folded_or_merged() -> None:
    tester = FakeTester()
    moved = TrainPR(number=2, head_sha="aaa111", current_head="bbb222")
    merger = FakeMerger()
    result = MergeTrain(tester=tester, repo="r").run([pr(1), moved], merger=merger)
    assert tester.called == [(1,)]
    assert merger.landed == [1]
    assert result.by_status(SKIPPED_MOVED)[0].pr == 2


def test_a_batch_of_only_stale_approvals_never_runs_a_test() -> None:
    tester = FakeTester()
    moved = TrainPR(number=2, head_sha="aaa111", current_head="bbb222")
    result = MergeTrain(tester=tester, repo="r").run([moved])
    assert tester.called == []
    assert result.landed == ()


# ---------------------------------------------------------------------------
# The green path
# ---------------------------------------------------------------------------


def test_a_green_batch_is_tested_once_and_lands_whole() -> None:
    tester = FakeTester()
    merger = FakeMerger()
    result = MergeTrain(tester=tester, repo="r").run([pr(1), pr(2), pr(3)], merger=merger)
    assert tester.called == [(1, 2, 3)]
    assert merger.landed == [1, 2, 3]
    assert result.landed == (1, 2, 3)
    assert result.set_aside == ()
    assert result.test_runs == 1


def test_landed_and_set_aside_partition_the_batch() -> None:
    tester = FakeTester(bad=frozenset({2}))
    result = MergeTrain(tester=tester, repo="r").run([pr(1), pr(2), pr(3)], merger=FakeMerger())
    judged = {v.pr for v in result.verdicts}
    assert judged == {1, 2, 3}
    assert set(result.landed) | set(result.set_aside) == {1, 2, 3}
    assert not set(result.landed) & set(result.set_aside)


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def test_a_conflict_sets_only_that_pr_aside_and_the_rest_still_lands() -> None:
    tester = FakeTester(conflicts=frozenset({2}))
    merger = FakeMerger()
    result = MergeTrain(tester=tester, repo="r").run([pr(1), pr(2), pr(3)], merger=merger)
    # The conflicted fold is retested without #2, rather than bisected.
    assert tester.called == [(1, 2, 3), (1, 3)]
    assert merger.landed == [1, 3]
    assert result.by_status(NEEDS_REBASE)[0].pr == 2
    assert "conflicts" in result.by_status(NEEDS_REBASE)[0].reason


def test_a_fully_conflicting_batch_lands_nothing() -> None:
    tester = FakeTester(conflicts=frozenset({1, 2}))
    merger = FakeMerger()
    result = MergeTrain(tester=tester, repo="r").run([pr(1), pr(2)], merger=merger)
    assert merger.landed == []
    assert result.landed == ()
    assert {v.pr for v in result.by_status(NEEDS_REBASE)} == {1, 2}


# ---------------------------------------------------------------------------
# The red path: bisection
# ---------------------------------------------------------------------------


def test_a_single_failing_pr_is_named_with_its_failing_tests() -> None:
    tester = FakeTester(bad=frozenset({1}))
    result = MergeTrain(tester=tester, repo="r").run([pr(1)])
    verdict = result.verdicts[0]
    assert verdict.status == NEEDS_REBASE
    assert verdict.failing_tests == tester.failing


def test_bisect_finds_the_culprit_and_the_rest_lands() -> None:
    prs = [pr(1), pr(2), pr(3), pr(4)]
    tester = FakeTester(bad=frozenset({2}))
    merger = FakeMerger()
    result = MergeTrain(tester=tester, repo="r").run(prs, merger=merger)
    assert result.landed == (1, 3, 4)
    culprit = result.by_status(REGRESSION)[0]
    assert culprit.pr == 2
    assert culprit.failing_tests == tester.failing


def test_bisect_costs_log_n_runs_not_n() -> None:
    # Four PRs, one bad.  Both halves are tested — a red left half says nothing
    # about the right one, so a short-circuit would ship the second culprit —
    # and only the red half is recursed into.  That is 1 + 2 + 2 + 1 runs, where
    # peeling one PR at a time would have taken five.
    tester = FakeTester(bad=frozenset({3}))
    result = MergeTrain(tester=tester, repo="r").run(
        [pr(1), pr(2), pr(3), pr(4)], merger=FakeMerger()
    )
    assert result.test_runs == 6
    assert len(tester.called) == 6
    assert result.landed == (1, 2, 4)
    assert [c.pr for c in result.by_status(REGRESSION)] == [3]


def test_bisect_never_exonerates_a_half_it_did_not_test() -> None:
    # A red left half says nothing about the right one, so the right half's
    # members are only cleared because they were themselves tested green.
    tester = FakeTester(bad=frozenset({1}))
    landable, culprits, runs = bisect([pr(1), pr(2), pr(3), pr(4)], tester)
    assert [p.number for p in landable] == [2, 3, 4]
    assert [c.pr for c in culprits] == [1]
    assert runs == len(tester.called)


def test_bisect_reports_every_pr_when_only_the_combination_fails() -> None:
    # Both halves are green but the pair is red: nobody is to blame alone, and
    # the batch must not land a combination already known to fail.
    tester = FakeTester(together=frozenset({1, 2}))
    merger = FakeMerger()
    result = MergeTrain(tester=tester, repo="r").run([pr(1), pr(2)], merger=merger)
    assert merger.landed == []
    assert [c.pr for c in result.by_status(REGRESSION)] == [1, 2]


def test_bisect_of_one_pr_names_it_directly() -> None:
    tester = FakeTester(bad=frozenset({7}))
    landable, culprits, runs = bisect([pr(7)], tester)
    assert landable == []
    assert [(c.pr, c.status) for c in culprits] == [(7, REGRESSION)]
    assert runs == 1


# ---------------------------------------------------------------------------
# The merge step
# ---------------------------------------------------------------------------


def test_a_pr_the_merger_declines_is_recorded_as_moved_not_landed() -> None:
    tester = FakeTester()
    merger = FakeMerger(decline=frozenset({2}))
    result = MergeTrain(tester=tester, repo="r").run([pr(1), pr(2)], merger=merger)
    assert result.landed == (1,)
    assert result.by_status(SKIPPED_MOVED)[0].pr == 2


def test_the_batch_is_merged_in_order_in_one_call() -> None:
    tester = FakeTester()
    merger = FakeMerger()
    MergeTrain(tester=tester, repo="r").run([pr(1), pr(2), pr(3)], merger=merger)
    assert merger.landed == [1, 2, 3]


def test_nothing_is_merged_when_there_is_no_merger() -> None:
    result = MergeTrain(tester=FakeTester(), repo="r").run([pr(1)])
    assert result.by_status(LANDED)[0].pr == 1
    assert result.landed == (1,)


# ---------------------------------------------------------------------------
# Test selection and the report
# ---------------------------------------------------------------------------


def test_changed_tests_are_selected_directly(tmp_path: Path) -> None:
    files = select_test_files([pr(1, files=("tests/test_a.py", "src/a.py"))], tree=tmp_path)
    assert files == ["tests/test_a.py"]


def test_a_changed_module_pulls_in_its_existing_sibling_test(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_thing.py").write_text("", encoding="utf-8")
    files = select_test_files([pr(1, files=("pkg/thing.py",))], tree=tmp_path)
    assert files == ["pkg/test_thing.py"]


def test_a_changed_module_with_no_sibling_test_adds_nothing(tmp_path: Path) -> None:
    assert select_test_files([pr(1, files=("pkg/thing.py",))], tree=tmp_path) == []


def test_non_python_changes_are_ignored_by_the_default_selector(tmp_path: Path) -> None:
    assert select_test_files([pr(1, files=("README.md", "docs/x.rst"))], tree=tmp_path) == []


def test_the_default_command_quotes_its_test_files() -> None:
    assert _test_command_for(["tests/a.py"]) == "pytest tests/a.py"
    assert _test_command_for([]) == "pytest"
    assert _test_command_for(["tests/a b.py"]) == "pytest 'tests/a b.py'"


def test_the_report_is_json_and_names_the_command(tmp_path: Path) -> None:
    import json

    report = tmp_path / "report.json"
    result = MergeTrain(tester=FakeTester(), repo="r").run([pr(1)])
    payload = result.to_dict()
    payload["command"] = "pytest -q"
    report.write_text(json.dumps(payload, default=str), encoding="utf-8")
    read = json.loads(report.read_text(encoding="utf-8"))
    assert read["landed"] == [1]
    assert read["command"] == "pytest -q"
    assert read["verdicts"][0]["status"] == LANDED


def test_render_text_reports_each_pr_and_the_tally() -> None:
    text = (
        MergeTrain(tester=FakeTester(bad=frozenset({2})), repo="r")
        .run([pr(1), pr(2)], merger=FakeMerger())
        .render_text()
    )
    assert "[OK] #1" in text
    assert "[REGRESSION] #2" in text
    assert "1 landed, 1 set aside" in text


# ---------------------------------------------------------------------------
# Integration: the real git fold
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


@dataclass
class OriginRepo:
    """A clone of a local ``origin`` carrying three PR branches."""

    clone: Path
    one: str
    two: str
    three: str

    def prs(self) -> list[TrainPR]:
        return [
            TrainPR(number=1, head_sha=self.one),
            TrainPR(number=2, head_sha=self.two),
            TrainPR(number=3, head_sha=self.three),
        ]


@pytest.fixture
def origin_repo(tmp_path: Path) -> OriginRepo:
    """A bare ``origin`` plus a clone, with two clean PRs and one conflicted PR."""
    upstream = tmp_path / "upstream"
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "T")
    (upstream / "README.md").write_text("base\n", encoding="utf-8")
    (upstream / "a.txt").write_text("a0\n", encoding="utf-8")
    (upstream / "b.txt").write_text("b0\n", encoding="utf-8")
    (upstream / "c.txt").write_text("c0\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "base")

    # PR 3 first, and on a file of its own: main moves on under it, so it
    # conflicts no matter which PRs the train folds before it.  Branching it
    # from a.txt — a file PR 1 also edits — would make the conflict a function
    # of the batch order instead of the PR's own staleness.
    _git(upstream, "checkout", "-q", "-b", "feat/conflict", "main")
    (upstream / "c.txt").write_text("from-pr3\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "three")
    three = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "checkout", "-q", "main")
    (upstream / "c.txt").write_text("moved-on\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "main moves on")

    # PR 1: a clean, unrelated change.
    _git(upstream, "checkout", "-q", "-b", "feat/one", "main")
    (upstream / "a.txt").write_text("a1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    one = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "checkout", "-q", "main")

    # PR 2: a second clean change, on its own branch.
    _git(upstream, "checkout", "-q", "-b", "feat/two", "main")
    (upstream / "b.txt").write_text("b1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "two")
    two = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "checkout", "-q", "main")

    _git(upstream, "clone", "-q", "--bare", ".", str(origin))
    _git(upstream, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    for branch in ("one", "two", "conflict"):
        _git(clone, "fetch", "-q", "origin", f"feat/{branch}:feat/{branch}")
    return OriginRepo(clone=clone, one=one, two=two, three=three)


def test_the_fold_sets_a_conflict_aside_and_keeps_the_rest(
    origin_repo: OriginRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real fold over a real repo: the conflicting PR is dropped, not fatal.

    Nothing is merged — ``gh`` is never on the path here — so this asserts the
    half of the run a fake cannot vouch for: that ``git merge --no-ff`` really
    does apply the clean PRs, set the conflicted one aside, and leave the test
    command looking at the combined tree.
    """
    monkeypatch.setenv("AGENT_FLEET_HOME", str(origin_repo.clone.parent / "home"))
    seen: list[tuple[str, ...]] = []

    def fake_run(
        argv: Sequence[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if argv[:1] == ["pytest"]:
            seen.append(tuple(sorted(p.name for p in Path(cwd).iterdir())))
            return subprocess.CompletedProcess(argv, 0, stdout="1 passed", stderr="")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    monkeypatch.setattr("agent_fleet.merge_plan.train._run", fake_run)
    trainer = GitTrainer(origin_repo.clone)
    result = MergeTrain(tester=trainer.evaluate, repo="demo").run(origin_repo.prs())

    # The test ran once, on a tree holding both clean PRs and not the conflict.
    assert len(seen) == 1
    assert "a.txt" in seen[0] and "b.txt" in seen[0]
    assert [v.pr for v in result.by_status(NEEDS_REBASE)] == [3]
    assert result.landed == (1, 2)
    # The operator's own checkout was never moved off main.
    assert _git(origin_repo.clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert origin_repo.clone.is_dir()


def test_run_train_writes_a_report_for_the_batch(
    origin_repo: OriginRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_FLEET_HOME", str(origin_repo.clone.parent / "home"))

    def fake_run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("agent_fleet.merge_plan.train._run", fake_run)
    report = origin_repo.clone.parent / "out" / "report.json"
    result = run_train(
        repo="demo",
        repo_path=origin_repo.clone,
        prs=origin_repo.prs(),
        report_path=report,
    )
    assert report.is_file()
    assert result.test_runs >= 1
    assert set(result.verdicts[0].to_dict()) == {"pr", "status", "reason", "failing_tests"}


def test_a_hanging_test_command_is_killed_and_counted_red(
    origin_repo: OriginRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test command that never returns is a test outcome, not a crash.

    Without the guard the timeout escapes ``evaluate``, unwinds through
    ``MergeTrain.run`` and ``run_train``, and abandons the run with no report —
    the candidate worktree left behind and no verdict for anything.
    """
    monkeypatch.setenv("AGENT_FLEET_HOME", str(origin_repo.clone.parent / "home"))

    def hang_on_tests_only(
        argv: Sequence[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        if argv[:1] == ["pytest"]:
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    # PR 3 is stale against main by construction, so it never reaches the test
    # run; the two clean PRs are what the hanging command has to survive.
    clean = [p for p in origin_repo.prs() if p.number != 3]

    monkeypatch.setattr("agent_fleet.merge_plan.train._run", hang_on_tests_only)
    trainer = GitTrainer(origin_repo.clone, command="pytest -q", timeout=1)

    hung = trainer.evaluate(clean)
    assert hung.passed is False
    assert hung.conflicts == ()
    assert "timeout" in hung.summary

    merger = FakeMerger()
    run = MergeTrain(tester=trainer.evaluate, repo="demo").run(clean, merger=merger)
    assert merger.landed == []
    assert {v.pr for v in run.verdicts} == {1, 2}
    assert [v.pr for v in run.by_status(REGRESSION)] == [1, 2]


# ---------------------------------------------------------------------------
# The CLI seam: which repository the train is about
# ---------------------------------------------------------------------------


def test_a_train_names_the_checkout_it_was_pointed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--repo-path`` decides the repository, not the first configured repo.

    fleet.yaml lists every repo the fleet knows about.  Taking its first entry
    would name a different repository than the operator passed, drop every
    approval belonging to the checkout they asked for, and report under the
    wrong name.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    config = tmp_path / "fleet.yaml"
    config.write_text(
        "merge_plan:\n  repos:\n    - name: other-repo\n      path: /somewhere/other\n",
        encoding="utf-8",
    )
    checkout = tmp_path / "agent-fleet"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "t@example.com")
    _git(checkout, "config", "user.name", "T")
    _git(checkout, "remote", "add", "origin", "https://github.com/Evan-Kim2028/agent-fleet.git")

    status = tmp_path / "status"
    status.mkdir()
    (status / "gate.md").write_text(
        "Evan-Kim2028/agent-fleet#12\nPREMERGE-APPROVED dc91fac\n", encoding="utf-8"
    )
    (status / "other.md").write_text(
        "other/other-repo#77\nPREMERGE-APPROVED deadbee\n", encoding="utf-8"
    )

    monkeypatch.setattr(GitHubClient, "for_repo", _stub_detail_client)
    args = argparse.Namespace(
        repo_path=str(checkout),
        repo=None,
        config=str(config),
        operator=None,
        status_dir=str(status),
        test_command="pytest",
        max_batch_size=5,
        report=None,
        dry_run=True,
        json=False,
    )
    assert merge_cli.cmd_merge_train(args) == 0
    out = capsys.readouterr().out
    assert "agent-fleet" in out
    assert "other-repo" not in out
    assert "#12" in out
    assert "#77" not in out


class StubDetailClient:
    """A ``GitHubClient`` whose every PR is open on ``feat/thing`` over main."""

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        del pr_number
        return {
            "state": "OPEN",
            "headRefOid": "dc91fac7c9233ec3da9e9",
            "headRefName": "feat/thing",
            "baseRefName": "main",
            "files": [{"path": "agent_fleet/thing.py"}],
        }


def _stub_detail_client(self: GitHubClient, repo_path: Path) -> StubDetailClient:
    del self, repo_path
    return StubDetailClient()
