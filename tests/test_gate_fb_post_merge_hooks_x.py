"""Gate tests for the post-merge hook: plan → label → trigger → hand off.

Each test pins one confirmed defect. The theme is *silent* failure: a batch that
reports success, carries real labels, and rebuilds nothing. The existing suite
injects ``MergedPR`` objects by hand, so the fetch→plan seam — where all of
these defects lived — was never exercised; the end-to-end tests here drive a
real ``gh`` and real ``plan.sh``/``rebuild.sh`` on disk instead.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.post_merge.config import RepoSpec
from agent_fleet.post_merge.flow import PR_FIELDS, _fetch_via_gh, run_post_merge
from agent_fleet.post_merge.handoff import Batch, write_note
from agent_fleet.post_merge.planner import PlanResult, plan_for_pr
from agent_fleet.post_merge.trigger import JobOutcome
from agent_fleet.post_merge.types import Job, MergedPR, Plan

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

#: Exactly what gh returns for a merged PR when asked for PR_FIELDS.
GH_PR_PAYLOAD = {
    "number": 1,
    "title": "widen the venue filter",
    "headRefOid": "h1",
    "mergeCommit": {"oid": "m1", "message": "Merge pull request #1"},
    "mergedAt": "2026-09-26T10:00:00Z",
    "files": ["transform/models/gold_sales.sql"],
    "labels": [{"name": "premerge-approved"}],
}

#: What the repo's plan_command prints for the payload above.
PLAN_JSON = json.dumps({"models": ["gold_sales"], "jobs": [{"job": "dbt"}]})


# ---------------------------------------------------------------- fakes


def _completed(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["fake"], returncode=rc, stdout=stdout, stderr=stderr)


class _SpyPlan:
    """Stands in for plan_command: records argv, stdin and cwd."""

    def __init__(self, stdout: str = PLAN_JSON) -> None:
        self.stdout = stdout
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        argv: Sequence[str],
        stdin: str,
        cwd: str = "",
        timeout: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": list(argv), "stdin": stdin, "cwd": cwd, "timeout": timeout})
        return _completed(self.stdout)


def _spec(tmp_path: Path, **kwargs: Any) -> RepoSpec:  # noqa: ANN401 - test builder
    defaults: dict[str, Any] = {
        "name": "r",
        "path": "",
        "plan_command": "plan.sh",
        "trigger_command": "",
        "state_dir": str(tmp_path / "state"),
        "handoff_inbox": str(tmp_path / "inbox"),
    }
    defaults.update(kwargs)
    return RepoSpec(**defaults)


def _pr(number: int = 1, **kwargs: Any) -> MergedPR:  # noqa: ANN401 - test builder
    defaults: dict[str, Any] = {
        "number": number,
        "title": "widen the venue filter",
        "head_sha": "h1",
        "merge_commit": "m1",
        "merged_at": "2026-09-26T10:00:00Z",
        "files": ("transform/models/gold_sales.sql",),
        "labels": (),
    }
    defaults.update(kwargs)
    return MergedPR(**defaults)


def _planned(number: int = 1) -> PlanResult:
    return PlanResult(
        _pr(number),
        Plan(models=("gold_sales",), jobs=(Job("dbt_sales", "s1"),)),
    )


def _install_fake_gh(tmp_path: Path, payload: object) -> Path:
    """Put a real executable named ``gh`` on PATH printing *payload* as JSON.

    The payload lives in its own file and the script just cats it, so no shell
    quoting can corrupt the JSON the fetcher is about to parse.
    """
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / "pr.json").write_text(json.dumps(payload), encoding="utf-8")
    gh = bindir / "gh"
    gh.write_text('#!/bin/sh\ncat "$(dirname "$0")/pr.json"\n', encoding="utf-8")
    gh.chmod(0o755)
    return bindir


def _use_fake_gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: object) -> None:
    """Point PATH at a fake ``gh`` and move the cwd away from every repo.

    A relative ``plan_command`` that still runs is therefore proof it resolved
    against the checkout, not against this process's cwd or PATH.
    """
    bindir = _install_fake_gh(tmp_path, payload)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.chdir(elsewhere)


def _write_script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _no_op_labeler() -> Callable[[int, Sequence[str], Sequence[str]], None]:
    """An ApplyFn that records nothing, for tests asserting on other steps."""

    def _label(pr: int, add: Sequence[str], remove: Sequence[str]) -> None:  # noqa: ARG001
        return None

    return _label


def _recording_labeler(
    seen: list[int],
) -> Callable[[int, Sequence[str], Sequence[str]], None]:
    """An ApplyFn that records the PR numbers it was asked to label."""

    def _label(pr: int, add: Sequence[str], remove: Sequence[str]) -> None:  # noqa: ARG001
        seen.append(pr)

    return _label


def _plan_script() -> str:
    """A plan.sh that prints the canned plan for the canned PR."""
    return f'#!/bin/sh\ncat > /dev/null\nprintf %s {json.dumps(PLAN_JSON)}\n'


# ------------------------------------------------------------------ all-1


def test_gh_payload_keeps_its_head_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """all-1: the fetcher asks gh for ``headRefOid``, so the PR must carry it.

    ``head_sha`` is the plan cache key *and* the planner's go/no-go. A "" there
    meant the planner was skipped and the PR was labelled rebuild:none.
    """
    _use_fake_gh(monkeypatch, tmp_path, GH_PR_PAYLOAD)

    pr = _fetch_via_gh(1, str(tmp_path))

    assert "headRefOid" in PR_FIELDS
    assert pr is not None
    assert pr.head_sha == "h1"
    assert pr.merge_commit == "m1"
    assert pr.labels == ("premerge-approved",)
    assert pr.files == ("transform/models/gold_sales.sql",)


def test_a_real_gh_batch_plans_and_queues_its_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """all-1 end-to-end: real gh, real plan.sh, real rebuild.sh, no injection.

    The plan the repo asked for must actually be applied to the forge and its
    job actually fire. Before the fix this printed ``rebuild:none``, queued
    nothing, and exited 0.
    """
    _use_fake_gh(monkeypatch, tmp_path, GH_PR_PAYLOAD)
    repo = tmp_path / "repo"
    _write_script(repo / "scripts" / "plan.sh", _plan_script())
    _write_script(repo / "scripts" / "rebuild.sh", "#!/bin/sh\nexit 0\n")
    spec = _spec(
        tmp_path,
        path=str(repo),
        plan_command="scripts/plan.sh",
        trigger_command="scripts/rebuild.sh {job}",
    )

    result = run_post_merge(spec, [1], deploy_rc=0, main_sha="main999")

    assert result.ok, result.errors
    assert result.plans[0].head_sha == "h1"
    assert result.plans[0].plan.models == ("gold_sales",)
    assert result.plans[0].plan.labels() == ("rebuild:light", "table:gold_sales")
    assert [(j.job, j.status) for j in result.jobs] == [("dbt", "ok")]
    assert result.note_path is not None
    index = (tmp_path / "inbox" / "INDEX").read_text(encoding="utf-8")
    assert "tier=light" in index
    assert "jobs=1" in index


# ------------------------------------------------------------------ all-2


def test_a_pr_with_no_head_sha_raises_instead_of_planning_nothing(tmp_path: Path) -> None:
    """all-2: "planner never ran" must not read as "nothing to rebuild"."""
    spy = _SpyPlan()

    with pytest.raises((RuntimeError, ValueError), match="head sha"):
        plan_for_pr(_spec(tmp_path), _pr(head_sha=""), run=spy)

    assert spy.calls == []


def test_a_pr_with_no_head_sha_is_never_labelled_rebuild_none(tmp_path: Path) -> None:
    """all-2: the forge must not be told rebuild:none when nothing was planned.

    ``rebuild:none`` is a real answer an operator reads. Applying it because the
    planner was skipped is strictly worse than applying nothing.
    """
    labelled: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []

    def _apply(pr: int, add: Sequence[str], remove: Sequence[str]) -> None:
        labelled.append((pr, tuple(add), tuple(remove)))

    def _fetch(number: int, repo_path: str) -> MergedPR:  # noqa: ARG001
        return _pr(number, head_sha="")

    result = run_post_merge(
        _spec(tmp_path), [1], deploy_rc=0, fetch=_fetch, run_plan=_SpyPlan(), apply=_apply
    )

    assert not result.ok
    assert result.plans == []
    assert "head sha" in result.errors[0]
    assert [add for _, add, _ in labelled if "rebuild:none" in add] == []


# ------------------------------------------------------------------ all-3


def test_a_tilde_repo_path_is_expanded_before_the_planner_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """all-3: ``path: ~/code/lake`` must work like every other path in the tree."""
    repo = tmp_path / "code" / "lake-of-rage"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    spy = _SpyPlan()

    plan_for_pr(_spec(tmp_path, path="~/code/lake-of-rage"), _pr(), run=spy)

    assert spy.calls[0]["cwd"] == str(repo)


def test_a_tilde_repo_path_actually_runs_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """all-3 end-to-end: the real runner, not a spy, against a ``~`` checkout.

    A literal ``~`` as cwd used to raise FileNotFoundError, which the flow does
    not catch — so the whole command aborted with a traceback and no results.
    """
    home = tmp_path / "home"
    repo = home / "code" / "lake-of-rage"
    _write_script(repo / "plan.sh", _plan_script())
    monkeypatch.setenv("HOME", str(home))
    spec = _spec(tmp_path, path="~/code/lake-of-rage", plan_command="./plan.sh")

    def _fetch(number: int, repo_path: str) -> MergedPR:  # noqa: ARG001
        return _pr(number)

    result = run_post_merge(
        spec, [1], deploy_rc=0, fetch=_fetch, apply=_no_op_labeler()
    )

    assert result.ok, result.errors
    assert result.plans[0].plan.models == ("gold_sales",)


# ------------------------------------------------------------------ all-4


def test_a_missing_trigger_binary_does_not_lose_the_handoff_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """all-4: the note records an outstanding rebuild even when the trigger dies.

    The downstream data agent learns about a pending rebuild from the note.
    An absent binary used to raise out of the flow, so the note and the INDEX
    were never written and the rebuild was simply forgotten.
    """
    _use_fake_gh(monkeypatch, tmp_path, GH_PR_PAYLOAD)
    repo = tmp_path / "repo"
    _write_script(repo / "plan.sh", _plan_script())
    # 'typo_rebuild.sh' is not on PATH: this is the mistyped-config case.
    spec = _spec(
        tmp_path, path=str(repo), plan_command="./plan.sh", trigger_command="typo_rebuild.sh {job}"
    )

    result = run_post_merge(
        spec, [1], deploy_rc=0, apply=_no_op_labeler()
    )

    assert [(j.job, j.status) for j in result.jobs] == [("dbt", "failed")]
    assert result.jobs[0].detail
    assert result.note_path is not None
    # The note names the outstanding job, and the batch is still indexed.
    assert "dbt (slot default): failed" in result.note_path.read_text(encoding="utf-8")
    assert "pr=1" in (tmp_path / "inbox" / "INDEX").read_text(encoding="utf-8")


def test_a_missing_trigger_binary_is_reported_as_a_failure(tmp_path: Path) -> None:
    """all-4: a lost rebuild is a failure the operator sees, not a silent success.

    The CLI turns a failed job into a non-zero exit, so what has to hold here is
    that the failure is *recorded* — the batch's own planning and labelling are
    untouched, and the job is reported rather than dropped.
    """
    spec = _spec(tmp_path, plan_command="plan.sh", trigger_command="typo_rebuild.sh {job}")
    labelled: list[int] = []

    def _fetch(number: int, repo_path: str) -> MergedPR:  # noqa: ARG001
        return _pr(number)

    result = run_post_merge(
        spec,
        [1],
        deploy_rc=0,
        fetch=_fetch,
        run_plan=_SpyPlan(),
        apply=_recording_labeler(labelled),
    )

    # Only the job failed: the PR was still planned and still labelled.
    assert result.errors == []
    assert result.missing == []
    assert labelled == [1]
    assert [(j.job, j.status) for j in result.jobs] == [("dbt", "failed")]
    # The CLI keys its non-zero exit off exactly this.
    assert any(j.status == "failed" for j in result.jobs)


# ------------------------------------------------------------------ all-5


def test_two_writes_in_the_same_second_get_distinct_notes(tmp_path: Path) -> None:
    """all-5: a retry must not overwrite the record of what actually ran.

    The ledger guarantees the retry: same second, same PRs, jobs now "skipped —
    already triggered", which overwrote the note saying they had run ok.
    """
    inbox = tmp_path / "inbox"
    first = Batch(
        repo="r",
        main_sha="main1",
        deploy_rc=0,
        results=[_planned()],
        outcomes=[JobOutcome("dbt_sales", "s1", "ok")],
    )
    second = Batch(
        repo="r",
        main_sha="main1",
        deploy_rc=0,
        results=[_planned()],
        outcomes=[JobOutcome("dbt_sales", "s1", "skipped", "already triggered")],
    )

    first_path = write_note(first, inbox)
    second_path = write_note(second, inbox)

    assert first_path != second_path
    assert "dbt_sales (slot s1): ok" in first_path.read_text(encoding="utf-8")
    assert "skipped — already triggered" in second_path.read_text(encoding="utf-8")
    index = (inbox / "INDEX").read_text(encoding="utf-8").splitlines()
    assert len(index) == 2
    assert "jobs=1" in index[0]
    assert "jobs=0" in index[1]


def test_a_content_identical_rewrite_is_a_no_op(tmp_path: Path) -> None:
    """all-5: a re-run with no new facts adds no second INDEX line."""
    inbox = tmp_path / "inbox"
    batch = Batch(
        repo="r",
        main_sha="main1",
        deploy_rc=0,
        results=[_planned()],
        outcomes=[JobOutcome("dbt_sales", "s1", "ok")],
    )

    first_path = write_note(batch, inbox)
    second_path = write_note(batch, inbox)

    assert first_path == second_path
    assert len((inbox / "INDEX").read_text(encoding="utf-8").splitlines()) == 1


# ------------------------------------------------------------------ all-6


def test_a_relative_plan_command_is_run_from_the_repo(tmp_path: Path) -> None:
    """all-6: ``plan_command: scripts/plan.sh`` means the repo's script.

    argv[0] must be anchored at the checkout, as the trigger runner already
    did — the two disagreed about what a relative path meant.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    spy = _SpyPlan()

    plan_for_pr(_spec(tmp_path, path=str(repo), plan_command="scripts/plan.sh"), _pr(), run=spy)

    assert spy.calls[0]["argv"][0] == str(repo / "scripts" / "plan.sh")
    assert spy.calls[0]["cwd"] == str(repo)


def test_a_relative_plan_command_is_path_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """all-6 end-to-end: a repo-relative planner runs from any cwd.

    The fleet process is nowhere near the checkout, so this can only pass if the
    program was resolved against ``spec.path`` rather than PATH.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    repo = tmp_path / "repo"
    _write_script(repo / "scripts" / "plan.sh", _plan_script())
    spec = _spec(tmp_path, path=str(repo), plan_command="scripts/plan.sh")

    plan = plan_for_pr(spec, _pr())

    assert plan.plan.models == ("gold_sales",)
    assert plan.plan.jobs == (Job("dbt"),)


def test_a_bare_plan_command_is_left_on_path(tmp_path: Path) -> None:
    """all-6: a command with no directory part is a PATH lookup, not a repo file."""
    spec = _spec(tmp_path, path=str(tmp_path / "repo"), plan_command="plan.sh")
    spy = _SpyPlan()

    plan_for_pr(spec, _pr(), run=spy)

    assert spy.calls[0]["argv"] == ["plan.sh"]


def test_plan_argv_quotes_survive_the_split(tmp_path: Path) -> None:
    """all-6: still no shell — a quoted argument is one argv entry."""
    spec = _spec(
        tmp_path, path=str(tmp_path / "repo"), plan_command='scripts/plan.sh --tag "a b"'
    )
    spy = _SpyPlan()

    plan_for_pr(spec, _pr(), run=spy)

    assert spy.calls[0]["argv"] == [
        str(tmp_path / "repo" / "scripts" / "plan.sh"),
        "--tag",
        "a b",
    ]


# ------------------------------------------------------------------ flow wiring


def test_a_batch_with_a_missing_pr_still_plans_and_hands_off(tmp_path: Path) -> None:
    """The happy path survives the stricter planner: one bad PR, one good one."""
    plan = json.dumps({"models": ["gold_sales"], "jobs": [{"job": "dbt"}]})

    def _fetch(number: int, repo_path: str) -> MergedPR | None:  # noqa: ARG001
        if number == 2:
            return None
        return _pr(1, head_sha="aaa111") if number == 1 else None

    result = run_post_merge(
        _spec(tmp_path),
        [1, 2],
        deploy_rc=0,
        fetch=_fetch,
        run_plan=_SpyPlan(plan),
        apply=_no_op_labeler(),
    )

    assert result.missing == [2]
    assert [p.pr_number for p in result.plans] == [1]
    assert result.plans[0].plan.models == ("gold_sales",)
    assert result.note_path is not None


def test_a_retry_does_not_double_fire_the_rebuild(tmp_path: Path) -> None:
    """The ledger still dedupes across batches, and both notes survive."""
    repo = tmp_path / "repo"
    _write_script(repo / "plan.sh", _plan_script())
    _write_script(repo / "rebuild.sh", "#!/bin/sh\nexit 0\n")
    spec = _spec(
        tmp_path,
        path=str(repo),
        plan_command="./plan.sh",
        trigger_command="./rebuild.sh {job}",
    )

    def _fetch(number: int, repo_path: str) -> MergedPR:  # noqa: ARG001
        return _pr(number)

    first = run_post_merge(
        spec, [1], deploy_rc=0, fetch=_fetch, apply=_no_op_labeler()
    )
    second = run_post_merge(
        spec, [1], deploy_rc=0, fetch=_fetch, apply=_no_op_labeler()
    )

    assert [(j.job, j.status) for j in first.jobs] == [("dbt", "ok")]
    # The ledger is what makes a retry a retry, not a double fire.
    assert [(j.job, j.status) for j in second.jobs] == [("dbt", "skipped")]
    assert first.note_path != second.note_path
    assert first.note_path.exists()
    assert second.note_path.exists()
