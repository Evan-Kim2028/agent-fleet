"""``fleet merge train`` must only land PRs this operator owns.

Claim (owner-1): a PR whose head branch is not this operator's is reported as
``SKIPPED-NOT-OWNED`` and is never folded, tested or merged.

On ``lor-main`` a dry run picked silph #4257, head branch ``dq1d/apidocs``.
That branch is ``documents-1d``'s, and documents-1d has its own shipper: the
train merged a PR that another session owns, and nothing in the run said so.  The
gate cannot catch it — a gate approves a *commit*, and it approves another
session's commits exactly as readily as this one's, so the branch is the only
fact that distinguishes them.

What must hold, and is pinned below:
- the default filter is this operator's own ``fb/`` lanes
- a foreign head is split out before the cap, the hold check and the fold
- the split is a partition, so every PR is either batched or reported
- the report names the skipped PR, its branch, and the filter that declined it
- a run with nothing owned says so, rather than reporting "nothing to run"

Drives the real ``HeadFilter``, the real ``MergeTrain`` and the real
``cmd_merge_train``.  No git, no network: fakes for the tester, the merger and
``gh``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.merge_plan.config import load_merge_train_spec, parse_merge_train_spec
from agent_fleet.merge_plan.train import (
    SKIPPED_NOT_OWNED,
    HeadFilter,
    MergeTrain,
    TrainPR,
    TrainResult,
)
from agent_fleet.merge_plan.train import TestResult as _TestResult

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from agent_fleet.merge_plan.collect import GitHubClient

# The two real branches from the incident: documents-1d's and ours.
DQ1D_HEAD = "dq1d/apidocs"
FB_HEAD = "fb/train-owner-filter"
#: The short SHA a gate status line records.
HEAD_SHA = "dc91fac7c9233ec3da9e9"


def pr(number: int, *, head: str = FB_HEAD, base: str = "main") -> TrainPR:
    return TrainPR(
        number=number,
        head_sha=f"sha{number}",
        base_ref=base,
        head_branch=head,
    )


# ---------------------------------------------------------------------------
# The filter itself: pure, no git, no network
# ---------------------------------------------------------------------------


def test_the_default_filter_is_this_operators_own_fb_lanes() -> None:
    """Out of the box the train owns ``fb/`` and nothing else.

    An empty default would make the filter opt-in, and the PR it was written for
    was picked up by a train nobody had configured.
    """
    head_filter = HeadFilter()

    assert head_filter.owns("fb/train-owner-filter")
    assert not head_filter.owns(DQ1D_HEAD), (
        "the default filter let another session's branch into the batch; that is "
        f"the defect this filter exists for (head {DQ1D_HEAD!r})"
    )


def test_a_foreign_head_branch_is_not_owned() -> None:
    """The incident, isolated: ``dq1d/*`` is documents-1d's, not this train's."""
    assert not HeadFilter().owns(DQ1D_HEAD)


def test_ownership_is_decided_by_prefix_not_by_repository() -> None:
    """A PR's repo says nothing about who owns its branch.

    documents-1d opens its PRs against the same repository this train runs on,
    so any ownership test keyed on the repo matches both.
    """
    head_filter = HeadFilter(include=("fb/", "hotfix/"))

    assert head_filter.owns("hotfix/urgent")
    assert not head_filter.owns("dq1d/apidocs")


def test_an_excluded_prefix_wins_over_an_included_one() -> None:
    """The two lists are not a set union; a deny must survive a match."""
    head_filter = HeadFilter(include=("fb/",), exclude=("fb/experimental/",))

    assert not head_filter.owns("fb/experimental/risky")
    assert head_filter.owns("fb/safe")


def test_a_pr_with_no_head_branch_is_not_owned() -> None:
    """An unnamed head is nobody's claim, so it is not this train's.

    ``gh`` returns ``headRefName: ""`` for a PR whose head is not readable; a
    filter that defaulted to "owned" would fold it on the strength of a field
    that is empty.
    """
    assert not HeadFilter().owns("")


def test_an_empty_include_list_owns_nothing() -> None:
    """ "Names nothing" must not read as "owns everything".

    The dangerous failure is a mistyped config leaving the include list empty and
    a filter that treats empty as "no restriction" — which lands every branch on
    the repository, the exact outcome the filter was added to prevent.
    """
    head_filter = HeadFilter(include=())

    assert not head_filter.owns(FB_HEAD)
    assert not head_filter.owns(DQ1D_HEAD)


def test_the_split_is_a_partition() -> None:
    """Every PR lands in exactly one half, or one is reported twice."""
    prs = [pr(1, head=FB_HEAD), pr(2, head=DQ1D_HEAD), pr(3, head=FB_HEAD), pr(4, head="")]

    owned, not_owned = HeadFilter().split(prs)

    numbers = [p.number for p in owned] + [p.number for p in not_owned]
    assert sorted(numbers) == [1, 2, 3, 4], "a PR was lost or counted twice by the split"
    assert [p.number for p in owned] == [1, 3]
    assert [p.number for p in not_owned] == [2, 4]


def test_the_split_does_not_depend_on_the_order_prs_arrive_in() -> None:
    """A report is the same run twice, whatever order the collectors found."""
    prs = [pr(3, head=DQ1D_HEAD), pr(1), pr(2, head=DQ1D_HEAD)]
    expected = [[p.number for p in half] for half in HeadFilter().split(prs)]

    for permutation in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
        reordered = [prs[i] for i in permutation]
        got = [[p.number for p in half] for half in HeadFilter().split(reordered)]
        assert got == expected, "the batch depends on the order the PRs arrived in"


# ---------------------------------------------------------------------------
# The report entry
# ---------------------------------------------------------------------------


def test_the_report_names_a_skipped_pr_its_branch_and_the_filter() -> None:
    """The report is the only place the operator finds out.

    A skipped PR that is not named, and not attributed to a branch, leaves the
    operator to guess whether the train ignored it or somebody else merged it.
    """
    result = TrainResult(repo="silphcoanalytics", not_owned=(pr(4257, head=DQ1D_HEAD),))
    head_filter = HeadFilter()

    (verdict,) = result.not_owned_verdicts(head_filter)

    assert verdict.pr == 4257
    assert verdict.status == SKIPPED_NOT_OWNED
    assert DQ1D_HEAD in verdict.reason, (
        f"the reason does not name the branch ({verdict.reason!r}); the operator "
        "cannot tell which session owns the PR without it"
    )
    assert "fb/" in verdict.reason, "the reason does not quote the filter that declined it"


def test_the_text_report_says_skipped_not_owned() -> None:
    """The report entry, as an operator reads it in a terminal."""
    result = TrainResult(
        repo="silphcoanalytics",
        ordered=(pr(12),),
        verdicts=(),
        not_owned=(pr(4257, head=DQ1D_HEAD),),
    )

    text = result.render_text()

    assert "SKIPPED-NOT-OWNED" in text
    assert "#4257" in text
    assert DQ1D_HEAD in text
    assert "skipped: not owned" in text


def test_the_json_report_carries_the_skipped_pr_outside_the_batch() -> None:
    """``not_owned`` is separate, so it cannot inflate the batch it did not run.

    A skipped PR listed in ``verdicts`` would make the landed / set_aside
    partition claim a verdict for a PR that was never folded or tested.
    """
    result = TrainResult(
        repo="silphcoanalytics",
        ordered=(pr(12),),
        verdicts=(),
        not_owned=(pr(4257, head=DQ1D_HEAD),),
    )

    payload = result.to_dict()

    assert payload["not_owned"] == [4257]
    assert payload["not_owned_count"] == 1
    assert payload["batch_size"] == 1
    assert payload["ordered"] == [12]
    # Present in the verdict stream so a reader scanning verdicts still sees it.
    assert 4257 in [v["pr"] for v in payload["verdicts"]]


def test_a_run_with_nothing_owned_says_so_rather_than_nothing_to_run() -> None:
    """Silence is how a foreign PR gets into the next train.

    "nothing to run" reads as an empty batch; the operator has no way to tell
    that the filter declined every PR they had approved.
    """
    result = TrainResult(repo="silphcoanalytics", not_owned=(pr(4257, head=DQ1D_HEAD),))

    text = result.render_text()

    assert "not owned" in text
    assert "nothing to run" in text


# ---------------------------------------------------------------------------
# The train: a foreign PR is never folded or tested
# ---------------------------------------------------------------------------


@dataclass
class RecordingTester:
    """A tester that records every batch it was handed, and passes them all."""

    seen: list[tuple[int, ...]] = field(default_factory=list)

    def __call__(self, batch: Sequence[TrainPR]) -> _TestResult:
        self.seen.append(tuple(p.number for p in batch))
        return _TestResult(passed=True)


@dataclass
class RecordingMerger:
    """A merger that records what it landed."""

    landed: list[tuple[int, ...]] = field(default_factory=list)

    def land(self, prs: Sequence[TrainPR]) -> dict[int, str]:
        self.landed.append(tuple(p.number for p in prs))
        return {p.number: "landed" for p in prs}


def test_a_foreign_pr_is_never_folded_tested_or_merged() -> None:
    """The claim, driven through the real train with fakes for git and gh."""
    tester = RecordingTester()
    merger = RecordingMerger()

    result = MergeTrain(tester=tester, repo="silphcoanalytics").run(
        [pr(12), pr(4257, head=DQ1D_HEAD)], merger=merger
    )

    assert tester.seen == [(12,)], (
        f"the train tested {tester.seen!r}; a PR on {DQ1D_HEAD!r} is another "
        "session's and must never enter a fold or a test run"
    )
    assert merger.landed == [(12,)], f"the train merged {merger.landed!r}"
    assert result.landed == (12,)
    assert [p.number for p in result.not_owned] == [4257]


def test_the_train_still_lands_the_prs_it_does_own() -> None:
    """A control: the filter must not stop the train's own work."""
    merger = RecordingMerger()

    result = MergeTrain(tester=RecordingTester(), repo="silphcoanalytics").run(
        [pr(12), pr(13)], merger=merger
    )

    assert merger.landed == [(12, 13)]
    assert result.landed == (12, 13)
    assert result.not_owned == ()


def test_a_batch_of_only_foreign_prs_runs_no_tests_at_all() -> None:
    """Nothing to fold means no worktree, no test run, no merge."""
    tester = RecordingTester()
    merger = RecordingMerger()

    result = MergeTrain(tester=tester, repo="silphcoanalytics").run(
        [pr(4257, head=DQ1D_HEAD)], merger=merger
    )

    assert tester.seen == [], "the train built and tested a tree of PRs it does not own"
    assert merger.landed == []
    assert result.landed == ()
    assert [p.number for p in result.not_owned] == [4257]
    assert result.test_runs == 0


def test_the_train_honours_a_wider_configured_filter() -> None:
    """A train configured for two prefixes lands both and skips the rest.

    Without this, an implementation that hard-codes ``fb/`` passes every other
    test here and silently ignores ``merge_train.include_head_prefixes``.
    """
    tester = RecordingTester()

    MergeTrain(
        tester=tester,
        repo="silphcoanalytics",
        head_filter=HeadFilter(include=("fb/", "dq1d/")),
    ).run([pr(12), pr(4257, head=DQ1D_HEAD)])

    assert tester.seen == [(12, 4257)]


# ---------------------------------------------------------------------------
# The config block
# ---------------------------------------------------------------------------


def test_a_missing_block_leaves_the_fb_default(tmp_path: Path) -> None:
    """A box with no fleet.yaml still gets a real ownership filter."""
    assert load_merge_train_spec(tmp_path / "absent.yaml").include_head_prefixes == ("fb/",)


def test_the_config_block_sets_both_lists(tmp_path: Path) -> None:
    config = tmp_path / "fleet.yaml"
    config.write_text(
        "merge_plan:\n"
        "  merge_train:\n"
        "    include_head_prefixes: ['fb/', 'hotfix/']\n"
        "    exclude_head_prefixes: ['fb/experimental/']\n",
        encoding="utf-8",
    )

    spec = load_merge_train_spec(config)

    assert spec.include_head_prefixes == ("fb/", "hotfix/")
    assert spec.exclude_head_prefixes == ("fb/experimental/",)


def test_a_mistyped_key_is_refused_rather_than_ignored() -> None:
    """A typo must not leave the default list silently in force.

    Falling back to ``fb/`` on a misspelled key looks exactly like a working
    config, so the operator would never learn the key is wrong.
    """
    with pytest.raises(ValueError, match="include_head_prefix"):
        parse_merge_train_spec({"include_head_branch_prefixes": ["fb/"]})


def test_a_non_list_prefix_list_is_refused() -> None:
    """``include_head_prefixes: fb/`` is a string, not a list of one."""
    with pytest.raises(ValueError, match="must be a list"):
        parse_merge_train_spec({"include_head_prefixes": "fb/"})


# ---------------------------------------------------------------------------
# The CLI seam
# ---------------------------------------------------------------------------


def test_a_flag_overrides_the_config_include_list() -> None:
    """``--include-head-prefix`` is an instruction, the config a declaration."""
    from agent_fleet.merge_plan import cli as merge_cli

    args = argparse.Namespace(include_head_prefix=["hotfix/"], exclude_head_prefix=None)

    head_filter = merge_cli._head_filter(args, None)

    assert head_filter.include == ("hotfix/",)


def test_a_flag_does_not_disturb_the_other_half_of_the_config() -> None:
    """Naming an include is not a statement that the excludes do not apply."""
    from agent_fleet.merge_plan import cli as merge_cli

    args = argparse.Namespace(include_head_prefix=["fb/"], exclude_head_prefix=None)

    head_filter = merge_cli._head_filter(args, None)

    assert head_filter.exclude == ()


def test_a_malformed_block_is_reported_rather_than_defaulted(tmp_path: Path) -> None:
    """A broken ``merge_train`` block must not silently become the default.

    The default is a real ownership filter, so a config that cannot be read has
    to be an error the operator sees.  Swallowing it would leave the train
    filtering by ``fb/`` while fleet.yaml says something else, and nothing in
    the run would say which one was in force.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    config = tmp_path / "fleet.yaml"
    config.write_text(
        "merge_plan:\n  merge_train:\n    include_head_branch_prefixes: ['fb/']\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(include_head_prefix=None, exclude_head_prefix=None)

    with pytest.raises(ValueError, match="merge_train"):
        merge_cli._head_filter(args, str(config))


def test_a_missing_config_is_not_an_error(tmp_path: Path) -> None:
    """A box with no fleet.yaml still gets the ``fb/`` default, not a refusal.

    ``_read_merge_plan_block`` swallows a missing file on purpose; this pins that
    the ownership filter does not turn that into a failure.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    args = argparse.Namespace(include_head_prefix=None, exclude_head_prefix=None)

    head_filter = merge_cli._head_filter(args, str(tmp_path / "absent.yaml"))

    assert head_filter.include == ("fb/",)


def test_the_parser_takes_repeated_prefix_flags() -> None:
    """Repeatable flags, so a second prefix is one more flag away.

    A single-valued flag would make owning several prefixes impossible without a
    config edit, and the config is per-box while the prefixes are per-repo.
    """
    import argparse as _argparse

    from agent_fleet.merge_plan.cli import register_merge_commands

    parser = _argparse.ArgumentParser()
    register_merge_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "merge",
            "train",
            "--repo-path",
            ".",
            "--include-head-prefix",
            "fb/",
            "--include-head-prefix",
            "hotfix/",
            "--exclude-head-prefix",
            "fb/experimental/",
        ]
    )

    assert args.include_head_prefix == ["fb/", "hotfix/"]
    assert args.exclude_head_prefix == ["fb/experimental/"]


def test_the_parser_gives_both_prefix_flags_a_default() -> None:
    """The flags must default to unset, so fleet.yaml is consulted.

    Defaulting ``--include-head-prefix`` to ``("fb/",)`` at the parser would
    make the config block dead: the flag's default would win on every run that
    did not name it, which is every run.
    """
    import argparse as _argparse

    from agent_fleet.merge_plan.cli import register_merge_commands

    parser = _argparse.ArgumentParser()
    register_merge_commands(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["merge", "train", "--repo-path", "."])

    assert args.include_head_prefix is None
    assert args.exclude_head_prefix is None


def test_no_flags_leaves_the_configured_filter_alone() -> None:
    """Neither flag given means fleet.yaml's answer, not the built-in default."""
    from agent_fleet.merge_plan import cli as merge_cli

    args = argparse.Namespace(include_head_prefix=None, exclude_head_prefix=None)

    head_filter = merge_cli._head_filter(args, None)

    assert head_filter.include == ("fb/",)
    assert head_filter.exclude == ()


class _ForeignPRClient:
    """A ``GitHubClient`` reporting one open PR on documents-1d's branch."""

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        del pr_number
        return {
            "state": "OPEN",
            "headRefOid": HEAD_SHA,
            "headRefName": DQ1D_HEAD,
            "baseRefName": "main",
            "files": [{"path": "a.txt"}],
        }


def _reporting_foreign_prs(
    client: _ForeignPRClient,
) -> Callable[[GitHubClient, Path], _ForeignPRClient]:
    """A ``GitHubClient.for_repo`` replacement handing back *client*."""

    def _factory(self: GitHubClient, repo_path: Path) -> _ForeignPRClient:
        del self, repo_path
        return client

    return _factory


def _one_foreign_approval(tmp_path: Path) -> None:
    """A gate status file approving silph #4257, the PR from the incident."""
    (tmp_path / "gate.md").write_text(
        f"Evan-Kim2028/silphcoanalytics#4257\nPREMERGE-APPROVED {HEAD_SHA}\n", encoding="utf-8"
    )


def _train_args(tmp_path: Path, *, dry_run: bool, json_out: bool) -> argparse.Namespace:
    return argparse.Namespace(
        repo_path=str(tmp_path),
        repo="silphcoanalytics",
        config=None,
        base_branch=None,
        operator=None,
        status_dir=str(tmp_path),
        test_command="pytest",
        max_batch_size=5,
        include_head_prefix=None,
        exclude_head_prefix=None,
        report=None,
        dry_run=dry_run,
        json=json_out,
    )


def test_a_dry_run_refuses_a_foreign_pr_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` is how this defect was found, so it must name the skip.

    A dry run that quietly drops the PR reads as a clean bill of health, and it
    is the last point at which the answer is free.
    """
    from agent_fleet.merge_plan import cli as merge_cli
    from agent_fleet.merge_plan.collect import GitHubClient

    monkeypatch.setattr("agent_fleet.merge_plan.collect.lanes_dir", lambda: tmp_path)
    monkeypatch.setattr(GitHubClient, "for_repo", _reporting_foreign_prs(_ForeignPRClient()))
    _one_foreign_approval(tmp_path)

    code = merge_cli.cmd_merge_train(_train_args(tmp_path, dry_run=True, json_out=False))

    err = capsys.readouterr().err
    assert code == 1, f"a run with nothing owned must not report success (exit {code})"
    assert "not owned" in err, f"the refusal did not say why; stderr was {err!r}"
    assert "#4257" in err, f"the refusal did not name the PR; stderr was {err!r}"
    assert DQ1D_HEAD in err, f"the refusal did not name the branch; stderr was {err!r}"


def test_a_refused_pr_never_reaches_the_train(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run is not called at all when the filter owns nothing.

    A refusal that still calls the trainer and hands it an empty batch still
    resolves a base branch, still takes the worktree lock, and reports a run
    that never happened.
    """
    from agent_fleet.merge_plan import cli as merge_cli
    from agent_fleet.merge_plan.collect import GitHubClient

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("agent_fleet.merge_plan.collect.lanes_dir", lambda: tmp_path)
    monkeypatch.setattr(GitHubClient, "for_repo", _reporting_foreign_prs(_ForeignPRClient()))
    monkeypatch.setattr(
        "agent_fleet.merge_plan.train.run_train",
        lambda **kwargs: calls.append(kwargs) or TrainResult(repo="silphcoanalytics"),
    )
    _one_foreign_approval(tmp_path)

    merge_cli.cmd_merge_train(_train_args(tmp_path, dry_run=False, json_out=True))

    capsys.readouterr()
    assert calls == [], (
        f"the train was handed {calls!r} for a batch it does not own; a refused PR "
        "must not be folded, tested or merged"
    )
