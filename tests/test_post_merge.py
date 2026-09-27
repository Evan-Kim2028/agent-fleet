"""Tests for the post-merge plan cache, label diffing, job dedupe, and notes.

Every external effect is faked: the planner is a callable, the forge is a dict,
the trigger records argv, and the inbox is ``tmp_path``. Nothing here touches
the network, ``gh``, or a real lake.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.post_merge.config import RepoSpec, load_repo_specs, resolve_repo_spec
from agent_fleet.post_merge.flow import run_post_merge
from agent_fleet.post_merge.handoff import Batch, index_line, render_note, write_note
from agent_fleet.post_merge.labels import apply_labels, diff_for
from agent_fleet.post_merge.planner import (
    PlanResult,
    cached_plan,
    dedupe_jobs,
    plan_for_pr,
)
from agent_fleet.post_merge.trigger import JobOutcome, read_ledger, render_trigger, run_jobs
from agent_fleet.post_merge.types import MergedPR, Plan, label_diff, parse_plan

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


# ---------------------------------------------------------------- fakes


def _completed(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["fake"], returncode=rc, stdout=stdout, stderr=stderr)


class _FakePlanner:
    """Stands in for plan_command: records stdin, returns canned stdout."""

    def __init__(self, stdout: str = "", stderr: str = "", rc: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.rc = rc
        self.calls: list[tuple[list[str], str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        stdin: str,
        cwd: str = "",  # noqa: ARG002 - signature must match RunFn
        timeout: int = 0,  # noqa: ARG002 - signature must match RunFn
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), stdin))
        return _completed(self.stdout, self.stderr, rc=self.rc)


class _Recorder:
    """Stands in for the forge label writer and the trigger runner."""

    def __init__(self) -> None:
        self.labels: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []
        self.triggers: list[list[str]] = []

    def label(self, pr: int, add: Sequence[str], remove: Sequence[str]) -> None:
        self.labels.append((pr, tuple(add), tuple(remove)))

    def trigger(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.triggers.append(list(argv))
        return _completed()


def _spec(tmp_path: Path, **kwargs: Any) -> RepoSpec:  # noqa: ANN401 - test builder
    defaults: dict[str, Any] = {
        "name": "lake-of-rage",
        "path": "",
        "plan_command": "plan.sh",
        "trigger_command": "trigger.sh {job} {slot}",
        "state_dir": str(tmp_path / "state"),
        "handoff_inbox": str(tmp_path / "inbox"),
    }
    defaults.update(kwargs)
    return RepoSpec(**defaults)


def _pr(number: int = 12, sha: str = "abc123", **kwargs: Any) -> MergedPR:  # noqa: ANN401 - test builder
    defaults: dict[str, Any] = {
        "number": number,
        "title": "widen venue filter",
        "head_sha": sha,
        "merge_commit": "merge" + sha,
        "merged_at": "2026-09-26T10:00:00Z",
        "files": ("transform/models/gold_sales.sql",),
        "labels": (),
    }
    defaults.update(kwargs)
    return MergedPR(**defaults)


# ---------------------------------------------------------------- plan caching


def test_plan_is_cached_per_head_sha(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    planner = _FakePlanner(json.dumps({"models": ["gold_sales"], "jobs": [{"job": "dbt"}]}))

    first = plan_for_pr(spec, _pr(), run=planner)
    second = plan_for_pr(spec, _pr(), run=planner)

    assert first.cached is False
    assert second.cached is True
    # The second read came from disk, so the planner ran exactly once.
    assert len(planner.calls) == 1
    assert second.plan == first.plan


def test_a_new_head_sha_replans(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    planner = _FakePlanner(json.dumps({"models": ["gold_sales"]}))

    plan_for_pr(spec, _pr(sha="aaa111"), run=planner)
    plan_for_pr(spec, _pr(sha="bbb222"), run=planner)

    assert len(planner.calls) == 2


def test_planner_receives_changed_paths_on_stdin(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    planner = _FakePlanner(json.dumps({"models": []}))
    pr = _pr(files=("a/b.sql", "c/d.sql"))

    plan_for_pr(spec, pr, run=planner)

    assert planner.calls[0][1] == "a/b.sql\nc/d.sql\n"


def test_cached_plan_returns_none_when_absent(tmp_path: Path) -> None:
    assert cached_plan(tmp_path, "deadbeef") is None


def test_unreadable_cache_entry_is_a_miss_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "junk.json").write_text("{not json", encoding="utf-8")
    assert cached_plan(tmp_path, "junk") is None


def test_failing_planner_raises_rather_than_planning_nothing(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    planner = _FakePlanner("", stderr="dbt exploded", rc=3)

    with pytest.raises(RuntimeError, match="dbt exploded"):
        plan_for_pr(spec, _pr(), run=planner)


def test_non_json_planner_output_is_an_error() -> None:
    """An empty plan would label the PR rebuild:none and silently skip it."""
    with pytest.raises(ValueError, match="valid JSON"):
        parse_plan("Traceback: something went wrong")


# ---------------------------------------------------------------- label diffing


def test_labels_cover_models_verify_and_tier() -> None:
    plan = Plan(models=("gold_sales", "gold_venues"), verify=("dim_venue",), heavy=True)
    assert plan.labels() == (
        "rebuild:heavy",
        "table:gold_sales",
        "table:gold_venues",
        "verify:dim_venue",
    )


def test_a_plan_with_no_models_is_rebuild_none() -> None:
    assert Plan().labels() == ("rebuild:none",)


def test_stale_labels_from_an_older_plan_are_removed() -> None:
    current = ("rebuild:light", "table:gold_venues", "table:gold_sales")
    # The plan now also wants a verify label, so it is added at the same time.
    desired = Plan(models=("gold_sales",), verify=("dim_venue",)).labels()

    delta = label_diff(current, desired)

    assert delta.add == ("verify:dim_venue",)
    assert delta.remove == ("table:gold_venues",)
    # rebuild:light is unchanged, so it is neither re-added nor removed.
    assert "rebuild:light" not in delta.add
    assert "rebuild:light" not in delta.remove


def test_unowned_labels_are_never_removed() -> None:
    """A human's labels and the gate's are not ours to strip."""
    current = ("bug", "premerge-approved", "rebuild:heavy", "table:gone")
    desired = Plan(models=("gold_sales",)).labels()

    delta = label_diff(current, desired)

    assert delta.remove == ("rebuild:heavy", "table:gone")
    assert "bug" not in delta.remove
    assert "premerge-approved" not in delta.remove


def test_diff_is_empty_when_labels_already_match() -> None:
    desired = Plan(models=("gold_sales",)).labels()
    assert diff_for(desired, desired).is_empty


def test_apply_makes_no_call_when_already_converged() -> None:
    recorder = _Recorder()
    desired = Plan(models=("gold_sales",)).labels()

    apply_labels(7, current=desired, desired=desired, apply=recorder.label)

    assert recorder.labels == []


def test_apply_applies_the_diff_once() -> None:
    recorder = _Recorder()
    desired = Plan(models=("gold_sales",), heavy=True).labels()

    apply_labels(7, current=("table:stale",), desired=desired, apply=recorder.label)

    assert recorder.labels == [(7, desired, ("table:stale",))]


# ---------------------------------------------------------------- job dedupe


def test_three_prs_touching_one_model_queue_one_job() -> None:
    def _result(job: str) -> PlanResult:
        return PlanResult(_pr(), Plan(jobs=(type("J", (), {"job": job, "slot": "s"})(),)))

    results = [_result("dbt_sales"), _result("dbt_sales"), _result("dbt_venues")]

    assert dedupe_jobs(results) == [("dbt_sales", "s"), ("dbt_venues", "s")]


def test_dedupe_keeps_first_seen_order() -> None:
    def _result(job: str, slot: str = "a") -> PlanResult:
        from agent_fleet.post_merge.types import Job

        return PlanResult(_pr(), Plan(jobs=(Job(job, slot),)))

    results = [_result("zebra"), _result("apple"), _result("zebra")]

    assert dedupe_jobs(results) == [("zebra", "a"), ("apple", "a")]


def test_jobs_run_once_per_batch(tmp_path: Path) -> None:
    spec = _spec(tmp_path, trigger_command="rebuild.sh {job}")
    recorder = _Recorder()

    # dedupe_jobs already collapsed the repeat; run_jobs still guards it, and
    # reports the in-batch duplicate as skipped rather than running it twice.
    outcomes = run_jobs(
        spec, [("dbt_sales", "s1"), ("dbt_sales", "s1"), ("dbt_venues", "s2")],
        deploy_rc=0, trigger=recorder.trigger,
    )

    assert recorder.triggers == [["rebuild.sh", "dbt_sales"], ["rebuild.sh", "dbt_venues"]]
    assert [o.status for o in outcomes] == ["ok", "skipped", "ok"]


def test_a_failed_deploy_queues_nothing(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    recorder = _Recorder()

    outcomes = run_jobs(spec, [("dbt_sales", "s1")], deploy_rc=1, trigger=recorder.trigger)

    assert recorder.triggers == []
    assert outcomes[0].status == "skipped"
    assert "deploy rc=1" in outcomes[0].detail


def test_a_job_already_triggered_is_not_run_again(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    recorder = _Recorder()
    run_jobs(spec, [("dbt_sales", "s1")], deploy_rc=0, trigger=recorder.trigger)

    # A second, overlapping batch must not double-fire the same rebuild.
    second = run_jobs(spec, [("dbt_sales", "s1")], deploy_rc=0, trigger=recorder.trigger)

    assert len(recorder.triggers) == 1
    assert second[0].status == "skipped"
    assert second[0].detail == "already triggered"


def test_a_failing_job_is_reported_and_not_recorded(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    spec_ledger = tmp_path / "led.txt"

    def _failing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:  # noqa: ARG001 - matches TriggerFn
        return _completed("", stderr="job blew up", rc=2)

    outcomes = run_jobs(
        spec, [("dbt_sales", "s1")], deploy_rc=0, trigger=_failing, ledger=spec_ledger
    )

    assert outcomes[0].status == "failed"
    assert outcomes[0].returncode == 2
    # Not recorded, so a later successful run can still pick it up.
    assert read_ledger(spec_ledger) == set()


def test_render_trigger_expands_job_and_slot() -> None:
    assert render_trigger("rebuild.sh {job} {slot}", job="dbt_sales", slot="s1") == [
        "rebuild.sh",
        "dbt_sales",
        "s1",
    ]


def test_missing_trigger_command_skips_rather_than_invents(tmp_path: Path) -> None:
    spec = _spec(tmp_path, trigger_command="")
    recorder = _Recorder()

    outcomes = run_jobs(spec, [("dbt_sales", "s1")], deploy_rc=0, trigger=recorder.trigger)

    assert recorder.triggers == []
    assert "no trigger_command configured" in outcomes[0].detail


# ---------------------------------------------------------------- note rendering


def _batch(**kwargs: Any) -> Batch:  # noqa: ANN401 - test builder
    defaults: dict[str, Any] = {
        "repo": "lake-of-rage",
        "main_sha": "main1234567890",
        "deploy_rc": 0,
        "results": [
            PlanResult(_pr(12, "aaa111"), Plan(models=("gold_sales",), verify=("dim_venue",))),
        ],
        "outcomes": [JobOutcome("dbt_sales", "s1", "ok")],
    }
    defaults.update(kwargs)
    return Batch(**defaults)


def test_note_carries_everything_the_downstream_agent_needs() -> None:
    note = render_note(_batch(), generated_at="2026-09-26T10:00:00Z")

    assert "# post-merge: lake-of-rage" in note
    assert "main12345" in note
    assert "deploy: ok" in note
    assert "rebuild tier: light" in note
    assert "### #12 widen venue filter" in note
    assert "aaa111" in note
    assert "mergeaaa111" in note
    assert "2026-09-26T10:00:00Z" in note
    assert "table:gold_sales" not in note  # tables are listed by name, not label
    assert "- gold_sales" in note
    assert "- dim_venue" in note
    assert "dbt_sales (slot s1): ok" in note


def test_a_table_that_is_rebuilt_is_not_also_verify_only() -> None:
    plan = Plan(models=("gold_sales",), verify=("gold_sales", "dim_venue"))
    note = render_note(_batch(results=[PlanResult(_pr(), plan)]), generated_at="t")

    section = note.split("## Verify-only tables")[1]
    assert "gold_sales" not in section
    assert "- dim_venue" in section


def test_note_reports_a_failed_deploy() -> None:
    note = render_note(_batch(deploy_rc=7), generated_at="t")
    assert "deploy: FAILED (rc=7)" in note


def test_batch_tier_is_heavy_if_any_pr_is_heavy() -> None:
    batch = _batch(
        results=[
            PlanResult(_pr(12, "a"), Plan(models=("a",))),
            PlanResult(_pr(13, "b"), Plan(models=("b",), heavy=True)),
        ]
    )
    assert batch.rebuild_tier == "heavy"


def test_empty_sections_say_none_rather_than_vanishing() -> None:
    note = render_note(
        _batch(results=[PlanResult(_pr(), Plan())], outcomes=[]), generated_at="t"
    )
    assert "- (none)" in note


def test_write_note_appends_exactly_one_index_line(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    batch = _batch(note_id="20260926T100000Z")

    path = write_note(batch, inbox)
    write_note(_batch(note_id="20260926T110000Z"), inbox)

    assert path.exists()
    index = (inbox / "INDEX").read_text(encoding="utf-8").splitlines()
    assert len(index) == 2
    assert "pr=12" in index[0]
    assert "tier=light" in index[0]
    assert "deploy=0" in index[0]
    assert "jobs=1" in index[0]


def test_index_line_counts_only_jobs_that_actually_ran() -> None:
    line = index_line(
        _batch(outcomes=[JobOutcome("a", "s", "ok"), JobOutcome("b", "s", "skipped")])
    )
    assert "jobs=1" in line


# ---------------------------------------------------------------- config


def test_config_loads_and_validates(tmp_path: Path) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text(
        "post_merge:\n"
        "  repos:\n"
        "    - name: lake-of-rage\n"
        "      plan_command: scripts/plan.sh\n"
        "      trigger_command: scripts/rebuild.sh {job}\n"
        "      handoff_inbox: ~/inbox\n",
        encoding="utf-8",
    )

    spec = load_repo_specs(cfg)["lake-of-rage"]

    assert spec.plan_command == "scripts/plan.sh"
    assert spec.trigger_command == "scripts/rebuild.sh {job}"
    assert spec.is_configured


def test_an_unknown_config_key_is_an_error(tmp_path: Path) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text(
        "post_merge:\n  repos:\n    - name: r\n      plan_command: p\n      plann_command: typo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown key"):
        load_repo_specs(cfg)


def test_an_unconfigured_repo_reports_itself_rather_than_guessing(tmp_path: Path) -> None:
    cfg = tmp_path / "fleet.yaml"
    cfg.write_text("post_merge:\n  repos: []\n", encoding="utf-8")

    spec = resolve_repo_spec("unheard-of", fleet_config_path=cfg)

    assert spec.is_configured is False
    assert spec.plan_command == ""


def test_missing_config_yields_no_specs(tmp_path: Path) -> None:
    assert load_repo_specs(tmp_path / "nope.yaml") == {}


# ---------------------------------------------------------------- end-to-end flow


def _fake_forge(prs: dict[int, MergedPR]) -> Callable[[int, str], MergedPR | None]:
    def _fetch(number: int, repo_path: str) -> MergedPR | None:  # noqa: ARG001 - matches FetchFn
        return prs.get(number)

    return _fetch


def test_the_whole_flow_plans_labels_triggers_and_hands_off(tmp_path: Path) -> None:
    spec = _spec(tmp_path, trigger_command="rebuild.sh {job}")
    recorder = _Recorder()
    plan = json.dumps(
        {"models": ["gold_sales"], "jobs": [{"job": "dbt_sales", "slot": "s1"}], "heavy": True}
    )
    prs = {
        12: _pr(12, "aaa111"),
        13: _pr(13, "bbb222", title="fix venue join"),
    }

    result = run_post_merge(
        spec,
        [12, 13],
        deploy_rc=0,
        main_sha="main999",
        fetch=_fake_forge(prs),
        run_plan=_FakePlanner(plan),
        apply=recorder.label,
        trigger=recorder.trigger,
    )

    assert result.ok
    assert [p.pr_number for p in result.plans] == [12, 13]
    # One job, even though both PRs named it.
    assert recorder.triggers == [["rebuild.sh", "dbt_sales"]]
    # Each PR was labelled to exactly its plan.
    assert all(add == ("rebuild:heavy", "table:gold_sales") for _, add, _ in recorder.labels)

    note = result.note_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "### #12 widen venue filter" in note
    assert "### #13 fix venue join" in note
    assert "rebuild tier: heavy" in note
    index = (tmp_path / "inbox" / "INDEX").read_text(encoding="utf-8")
    assert "pr=12,13" in index
    assert "jobs=1" in index


def test_a_failed_deploy_still_writes_the_note_but_queues_nothing(tmp_path: Path) -> None:
    spec = _spec(tmp_path, trigger_command="rebuild.sh {job}")
    recorder = _Recorder()
    plan = json.dumps({"models": ["gold_sales"], "jobs": [{"job": "dbt_sales", "slot": "s1"}]})

    result = run_post_merge(
        spec,
        [12],
        deploy_rc=9,
        main_sha="main999",
        fetch=_fake_forge({12: _pr()}),
        run_plan=_FakePlanner(plan),
        apply=recorder.label,
        trigger=recorder.trigger,
    )

    assert recorder.triggers == []
    # The note is the record that a rebuild is outstanding but was not started.
    assert "deploy: FAILED (rc=9)" in result.note_path.read_text(encoding="utf-8")  # type: ignore[union-attr]


def test_one_bad_plan_does_not_strand_the_rest_of_the_batch(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    recorder = _Recorder()
    good = json.dumps({"models": ["gold_sales"]})
    calls = {"n": 0}

    def _sometimes_fails(
        argv: Sequence[str] = (),  # noqa: ARG001 - signature must match RunFn
        stdin: str = "",  # noqa: ARG001 - signature must match RunFn
        cwd: str = "",  # noqa: ARG001 - signature must match RunFn
        timeout: int = 0,  # noqa: ARG001 - signature must match RunFn
    ) -> subprocess.CompletedProcess[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _completed("", "planner died", rc=1)
        return _completed(good)

    result = run_post_merge(
        spec,
        [12, 13],
        deploy_rc=0,
        fetch=_fake_forge({12: _pr(12, "aaa111"), 13: _pr(13, "bbb222")}),
        run_plan=_sometimes_fails,
        apply=recorder.label,
        trigger=recorder.trigger,
    )

    assert not result.ok
    assert len(result.errors) == 1
    assert "planner died" in result.errors[0]
    # The surviving PR still got labelled and handed off.
    assert [p.pr_number for p in result.plans] == [13]
    assert result.note_path is not None


def test_an_unreadable_pr_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    result = run_post_merge(
        spec,
        [12, 13],
        deploy_rc=0,
        fetch=_fake_forge({12: _pr()}),
        run_plan=_FakePlanner(json.dumps({"models": []})),
    )

    assert not result.ok
    assert result.missing == [13]


def test_no_handoff_suppresses_the_note(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    result = run_post_merge(
        spec,
        [12],
        deploy_rc=0,
        fetch=_fake_forge({12: _pr()}),
        run_plan=_FakePlanner(json.dumps({"models": []})),
        write_notes=False,
    )

    assert result.note_path is None
    assert not (tmp_path / "inbox" / "INDEX").exists()


def test_labels_are_written_even_when_no_labeler_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: apply=None must mean "the real gh labeler", not "skip".

    Skipping labelling by default is the silent bug where a PR is planned and
    rebuilt but never carries its labels, so the default is asserted here.
    """
    recorder = _Recorder()
    monkeypatch.setattr(
        "agent_fleet.post_merge.flow.gh_labeler", lambda cwd="": recorder.label  # noqa: ARG005
    )

    run_post_merge(
        _spec(tmp_path),
        [12],
        deploy_rc=0,
        fetch=_fake_forge({12: _pr()}),
        run_plan=_FakePlanner(json.dumps({"models": ["gold_sales"]})),
        write_notes=False,
    )

    assert [c[0] for c in recorder.labels] == [12]
    assert "table:gold_sales" in recorder.labels[0][1]
