"""The branch tested and the branch merged into must be the same one.

Claim (prodsafety-2): ``resolve_base_branch`` returns the operator/config
supplied branch without checking it against the branch each PR actually
targets, and ``GitMerger.land`` never passes ``--repo`` or
``--match-head-commit`` — so the train tests one base and merges into another.

Reproduce it: ``fleet merge train --base-branch develop`` over a repository
whose approved PRs all target ``main``.  ``resolve_base_branch`` returns
``develop``, the whole batch is folded and tested against ``origin/develop``,
and then ``gh pr merge <n> --merge`` merges each PR into its real base
``main`` — a tree that was never combined with ``origin/main``.

Both halves are asserted here against the real functions: the base resolution
silently accepts a branch the batch contradicts, and the merge argv carries
neither ``--match-head-commit`` (which would pin the approved SHA) nor
``--repo`` (which would make the merge target unambiguous).
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.merge_plan.train import (
    GitMerger,
    TrainPR,
    resolve_base_branch,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_a_configured_base_is_not_checked_against_the_prs_real_base(tmp_path: Path) -> None:
    """The claimed defect: the configured branch wins even when the PRs disagree.

    The approved PRs all target ``main``; the operator (or a stale fleet.yaml
    entry) says ``develop``.  ``resolve_base_branch`` must not hand ``develop``
    back as though the batch merges into it.
    """
    prs = [
        TrainPR(number=1, head_sha="sha1", base_ref="main", head_branch="feat/one"),
        TrainPR(number=2, head_sha="sha2", base_ref="main", head_branch="feat/two"),
    ]

    resolved = resolve_base_branch(tmp_path, configured="develop", prs=prs)

    # The only defensible answers are the branch the PRs declare ("main") or a
    # refusal.  "develop" folds onto a branch the PRs do not merge into.
    assert resolved != "develop", (
        f"resolve_base_branch returned {resolved!r} for PRs that all target 'main'; "
        "the configured branch was accepted without checking the batch's real base, "
        "so the train would test against develop and merge into main"
    )


def test_a_configured_base_must_not_silently_contradict_the_batch(tmp_path: Path) -> None:
    """State the contradiction the way an operator would see it.

    A batch that unambiguously targets ``main`` and a configured ``develop`` is
    exactly the drift the claim describes: the fold cuts the candidate from
    ``origin/develop`` and the merge lands into ``main``.
    """
    prs = [TrainPR(number=1, head_sha="sha1", base_ref="main", head_branch="feat/one")]

    with pytest.raises(ValueError, match="develop|main|base"):
        resolve_base_branch(tmp_path, configured="develop", prs=prs)


def test_the_merge_command_pins_the_approved_head(tmp_path: Path) -> None:
    """``gh pr merge`` must name the commit the gate approved and the test ran.

    Without ``--match-head-commit <sha>`` the merge is a bare "merge whatever is
    the head now", so a head that moved in the window between the test run and
    the merge is merged anyway — the exact hazard the module docstring claims
    the re-check prevents.  ``--repo`` makes the target unambiguous instead of
    resolved against the checkout's remote.
    """
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"state": "OPEN", "headRefOid": "sha1"}),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    import agent_fleet.merge_plan.train as train_mod

    original = train_mod._run
    train_mod._run = fake_run  # type: ignore[assignment]
    try:
        merger = GitMerger(tmp_path)
        landed = merger.land([TrainPR(number=7, head_sha="sha1")])
    finally:
        train_mod._run = original  # type: ignore[assignment]

    assert landed == [7], f"precondition: the PR was merged, got {landed}"

    merge_calls = [c for c in calls if c[:3] == ["gh", "pr", "merge"]]
    assert merge_calls, "precondition: gh pr merge was issued"
    argv = merge_calls[0]

    assert "--match-head-commit" in argv, (
        f"gh pr merge issued without --match-head-commit: {argv}; a head that moved "
        "between the test run and the merge is merged unreviewed"
    )
    assert "--repo" in argv, (
        f"gh pr merge issued without --repo: {argv}; the merge target is resolved "
        "against the checkout's remote rather than the PR's repository"
    )


def test_a_head_that_moved_is_not_merged(tmp_path: Path) -> None:
    """Control: the pre-merge head re-check is real, so the fix has a seam.

    The claim is not that the head check is missing — it is that the merge
    itself is unguarded.  This shows the check works, so ``--match-head-commit``
    is the missing second lock, not a replacement for the first.
    """
    calls: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"state": "OPEN", "headRefOid": "deadbeef999"}),
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    import agent_fleet.merge_plan.train as train_mod

    original = train_mod._run
    train_mod._run = fake_run  # type: ignore[assignment]
    try:
        merger = GitMerger(tmp_path)
        landed = merger.land([TrainPR(number=7, head_sha="sha1")])
    finally:
        train_mod._run = original  # type: ignore[assignment]

    assert landed == [], "precondition: a moved head is not merged (this part is correct)"
    assert not [c for c in calls if c[:3] == ["gh", "pr", "merge"]], (
        "a moved head must not reach gh pr merge at all"
    )


def test_the_folded_branch_and_the_prs_base_are_the_same(tmp_path: Path) -> None:
    """Whatever branch is resolved, it must be one the batch merges into.

    This is the invariant the claim says is broken: the resolved base and the
    PRs' declared base must agree, or the train tests a tree that will never be
    the tree that lands.
    """
    prs = [
        TrainPR(number=1, head_sha="sha1", base_ref="main", head_branch="feat/one"),
        TrainPR(number=2, head_sha="sha2", base_ref="main", head_branch="feat/two"),
    ]
    resolved = resolve_base_branch(tmp_path, configured="develop", prs=prs)
    declared = {p.base_ref for p in prs}
    assert resolved in declared, (
        f"the train folds onto {resolved!r} but the batch merges into {declared}; "
        "the tested tree and the merged tree are different"
    )
