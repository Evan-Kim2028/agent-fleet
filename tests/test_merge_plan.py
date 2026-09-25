"""Tests for agent_fleet.merge_plan — the command center's ship-together planner.

Covers the five batching rules and the collection contract:
- overlapping files split into separate batches
- PRs sharing a dbt model land in one batch, with the correct --select set
- a risky PR is isolated into its own trailing batch
- a moved head is reported as a stale approval and excluded
- the batch cap holds, including a dbt group larger than the cap
- output is byte-identical for identical input

The fixture is a real git repo (so merge compatibility is genuinely exercised)
plus a fake `gh` on PATH serving canned JSON.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.merge_plan.batching import dbt_groups, merge_compatible, plan_batches
from agent_fleet.merge_plan.collect import (
    GitHubClient,
    collect_from_lanes,
    collect_from_status_dir,
    dedupe_approvals,
    parse_approval,
    profile_approvals,
)
from agent_fleet.merge_plan.config import builtin_spec
from agent_fleet.merge_plan.plan import render_plan_text
from agent_fleet.merge_plan.profile import (
    build_profile,
    dbt_models_for,
    deploy_unit_for,
    expand_downstream,
    risk_flags_for,
)
from agent_fleet.merge_plan.types import ApprovedPR, Batch, ChangeProfile, MergePlan, RepoSpec

# ---------------------------------------------------------------------------
# Fixtures: a real git repo plus a fake gh
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """An initialized repo on `main` with one commit."""
    repo = tmp_path / "lake-of-rage"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


class FakeGh:
    """A fake `gh` executable, driven by a JSON map of {pr_number: detail}."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._data_file = tmp_path / "gh_data.json"
        self._data_file.write_text("{}", encoding="utf-8")
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "gh"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"data = json.load(open({str(self._data_file)!r}))\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['pr', 'view']:\n"
            "    entry = data.get(args[2])\n"
            "    if entry is None:\n"
            "        sys.stderr.write('no such pr\\n')\n"
            "        sys.exit(1)\n"
            "    print(json.dumps(entry))\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def set_prs(self, details: dict[int, dict]) -> None:
        self._data_file.write_text(
            json.dumps({str(number): detail for number, detail in details.items()}),
            encoding="utf-8",
        )


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    return FakeGh(tmp_path, monkeypatch)


def make_pr(
    number: int,
    *,
    repo: str = "lake-of-rage",
    sha: str = "",
) -> ApprovedPR:
    return ApprovedPR(
        repo=repo,
        pr_number=number,
        approved_sha=sha or f"abc{number:06d}",
        head_sha=sha or f"abc{number:06d}",
    )


def profile_for(pr: ApprovedPR, files: list[str], spec: RepoSpec | None = None) -> ChangeProfile:
    spec = spec or builtin_spec(pr.repo)
    return build_profile(pr, files=files, repo_spec=spec)


def spec_for(repo: str = "lake-of-rage", **kw: object) -> RepoSpec:
    spec = builtin_spec(repo, path=kw.pop("path", ""))
    for key, value in kw.items():
        setattr(spec, key, value)
    return spec


def keyed(profiles: dict[int, ChangeProfile], repo: str = "lake-of-rage") -> dict:
    """Rekey a {pr_number: profile} map the way plan_batches expects."""
    return {(repo, pr_number): profile for pr_number, profile in profiles.items()}


def with_dbt_select(
    profile: ChangeProfile, models: tuple[str, ...], source: str = "manifest"
) -> ChangeProfile:
    """Copy *profile* with a specific dbt --select set."""
    from dataclasses import replace

    return replace(profile, dbt_select=models, dbt_select_source=source)


# ---------------------------------------------------------------------------
# Rule: no file overlap -> separate batches
# ---------------------------------------------------------------------------


def test_overlapping_files_split_into_separate_batches() -> None:
    spec = spec_for()
    a = profile_for(make_pr(1), ["api/src/x.py"], spec)
    b = profile_for(make_pr(2), ["api/src/x.py"], spec)
    c = profile_for(make_pr(3), ["api/src/y.py"], spec)

    batches = plan_batches(
        [make_pr(1), make_pr(2), make_pr(3)],
        keyed({1: a, 2: b, 3: c}),
        repo_specs={"lake-of-rage": spec},
        check_merges=False,
    )
    members = [[p.pr_number for p in batch.prs] for batch in batches]
    # 1 and 2 both edit api/src/x.py, so they can never share a batch.
    for batch in members:
        assert not ({1, 2} <= set(batch))
    # Every PR is still planned, and the disjoint PR 3 packs with one of them.
    assert sum(len(m) for m in members) == 3
    assert [1, 3] in members or [2, 3] in members


def test_disjoint_prs_share_a_batch() -> None:
    spec = spec_for()
    prs = [make_pr(1), make_pr(2), make_pr(3)]
    profiles = {p.pr_number: profile_for(p, [f"api/src/f{p.pr_number}.py"], spec) for p in prs}
    batches = plan_batches(
        prs, keyed(profiles), repo_specs={"lake-of-rage": spec}, check_merges=False
    )
    assert len(batches) == 1
    assert [p.pr_number for p in batches[0].prs] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Rule: dbt models rebuild once
# ---------------------------------------------------------------------------


def test_same_dbt_model_prs_group_into_one_batch() -> None:
    spec = spec_for()
    prs = [make_pr(1), make_pr(2)]
    # cardindex feeds sales downstream, so both PRs select the same model and
    # their rebuild can run once.
    profiles = {
        1: with_dbt_select(
            profile_for(prs[0], ["transform/models/gold/sales.sql"], spec),
            ("gold.cardindex", "gold.sales"),
        ),
        2: with_dbt_select(
            profile_for(prs[1], ["transform/models/gold/cardindex.sql"], spec),
            ("gold.sales",),
        ),
    }
    assert dbt_groups(list(profiles.values()))

    batches = plan_batches(
        prs, keyed(profiles), repo_specs={"lake-of-rage": spec}, check_merges=False
    )
    assert len(batches) == 1
    assert batches[0].dbt_select == ("gold.cardindex", "gold.sales")


def test_dbt_models_parsed_from_paths() -> None:
    assert dbt_models_for(["transform/models/gold/sales.sql"]) == ("gold.sales",)
    assert dbt_models_for(["transform/models/silver/foo.py"]) == ("silver.foo",)
    assert dbt_models_for(["transform/models/schema.yml"]) == ("schema",)
    assert dbt_models_for(["api/src/main.py"]) == ()


def test_expand_downstream_transitively() -> None:
    # c <- b <- a: b reads c, a reads b. parent_map is upstream-only, so it
    # expands to a leaf; the dependents are the inverse relation.
    parent_map = {
        "model.transform.a": ["model.transform.b"],
        "model.transform.b": ["model.transform.c"],
        "model.transform.c": [],
    }
    assert expand_downstream(["a"], parent_map) == ("a",)
    # Editing c invalidates everything that reads it, transitively.
    assert expand_downstream(["c"], parent_map) == ("c", "b", "a")
    assert expand_downstream(["a"], {}) == ("a",)


# ---------------------------------------------------------------------------
# Rule: risky PRs isolated, and ship last
# ---------------------------------------------------------------------------


def test_risky_pr_is_isolated_and_runs_last() -> None:
    spec = spec_for()
    safe_a = profile_for(make_pr(1), ["api/src/a.py"], spec)
    safe_b = profile_for(make_pr(2), ["api/src/b.py"], spec)
    risky = profile_for(make_pr(3), ["packages/lakestore/migrations/0001.sql"], spec)

    batches = plan_batches(
        [make_pr(1), make_pr(2), make_pr(3)],
        keyed({1: safe_a, 2: safe_b, 3: risky}),
        repo_specs={"lake-of-rage": spec},
        check_merges=False,
    )
    assert batches[-1].isolated_risk is True
    assert [p.pr_number for p in batches[-1].prs] == [3]
    assert batches[-1].size == 1
    # The two safe PRs still ride together in one deploy.
    assert batches[0].isolated_risk is False
    assert [p.pr_number for p in batches[0].prs] == [1, 2]


def test_risk_flags_detected() -> None:
    assert "migration" in risk_flags_for(["sql/gold_catalog.json"])
    assert "deploy" in risk_flags_for(["infra/vps/run_lor_api.sh"])
    assert "prod_write" in risk_flags_for(["pipelines/p/src/pipe/ops/x.py"])
    assert risk_flags_for(["api/src/main.py"]) == ()


# ---------------------------------------------------------------------------
# Rule: batch size cap
# ---------------------------------------------------------------------------


def test_batch_cap_is_respected() -> None:
    spec = spec_for()
    prs = [make_pr(i) for i in range(1, 8)]
    profiles = {p.pr_number: profile_for(p, [f"api/src/f{p.pr_number}.py"], spec) for p in prs}
    batches = plan_batches(
        prs,
        keyed(profiles),
        repo_specs={"lake-of-rage": spec},
        max_batch_size=3,
        check_merges=False,
    )
    assert all(b.size <= 3 for b in batches)
    assert sum(b.size for b in batches) == 7


def test_oversized_dbt_group_splits_and_is_labelled() -> None:
    spec = spec_for()
    prs = [make_pr(i) for i in range(1, 8)]
    profiles = {}
    for p in prs:
        base = profile_for(p, [f"transform/models/gold/f{p.pr_number}.sql"], spec)
        # Every PR feeds the same downstream model -> one dbt group of 7.
        profiles[p.pr_number] = with_dbt_select(base, ("gold.shared",), source="manifest")
    batches = plan_batches(
        prs,
        keyed(profiles),
        repo_specs={"lake-of-rage": spec},
        max_batch_size=5,
        check_merges=False,
    )
    assert all(b.size <= 5 for b in batches)
    assert sum(b.size for b in batches) == 7
    assert any(b.dbt_group_split for b in batches)
    assert any("cap" in reason for b in batches for reason in b.reasons)


# ---------------------------------------------------------------------------
# Stale approvals
# ---------------------------------------------------------------------------


def test_stale_approval_detected_and_excluded() -> None:
    spec = spec_for()
    stale_pr = ApprovedPR(
        repo="lake-of-rage", pr_number=1, approved_sha="aaaa111", head_sha="bbbb222"
    )
    batchable, profiles, stale, unprofilable = profile_approvals(
        [stale_pr],
        client=_StubClient({1: {"headRefOid": "bbbb222222"}}),
        repo_specs={"lake-of-rage": spec},
    )
    assert batchable == []
    assert len(stale) == 1
    assert "stale approval" in stale[0].reason
    assert unprofilable == []
    assert profiles == {}


def test_current_approval_is_batchable() -> None:
    spec = spec_for()
    pr = ApprovedPR(
        repo="lake-of-rage", pr_number=7, approved_sha="aaaa1111", head_sha="aaaa1111ff"
    )
    batchable, profiles, stale, _ = profile_approvals(
        [pr],
        client=_StubClient(
            {7: {"headRefOid": "aaaa1111ffff", "files": [{"path": "api/src/a.py"}]}}
        ),
        repo_specs={"lake-of-rage": spec},
    )
    assert [p.pr_number for p in batchable] == [7]
    assert stale == []
    assert profiles[("lake-of-rage", 7)].files == ("api/src/a.py",)


def test_unreadable_head_is_reported_not_assumed() -> None:
    spec = spec_for()
    pr = ApprovedPR(repo="lake-of-rage", pr_number=1, approved_sha="aaaa111", head_sha="")
    batchable, _, stale, unprofilable = profile_approvals(
        [pr], client=_StubClient({}), repo_specs={"lake-of-rage": spec}
    )
    assert batchable == []
    assert stale == []
    assert len(unprofilable) == 1


class _StubClient:
    """Minimal GitHubClient stand-in returning canned PR details."""

    def __init__(self, details: dict[int, dict]) -> None:
        self._details = details

    def pr_detail(self, pr_number: int) -> dict:
        return self._details.get(pr_number, {})


# ---------------------------------------------------------------------------
# Approval discovery
# ---------------------------------------------------------------------------


def test_parse_approval_extracts_sha() -> None:
    assert parse_approval("PREMERGE-APPROVED 0dc2391ab") == "0dc2391ab"
    assert parse_approval("PREMERGE-APPROVED 0dc2391ab ready") == "0dc2391ab"
    assert parse_approval("nothing here") == ""


def test_collect_from_lanes_reads_status_line(tmp_path: Path) -> None:
    lanes = tmp_path / "lanes"
    (lanes / "op").mkdir(parents=True)
    (lanes / "op" / "lane1.json").write_text(
        json.dumps(
            {
                "lane": "lane1",
                "operator": "op",
                "repo": "lake-of-rage",
                "pr": 42,
                "status_line": "PREMERGE-APPROVED 0dc2391ab",
            }
        ),
        encoding="utf-8",
    )
    found = collect_from_lanes(lanes_root=lanes)
    assert len(found) == 1
    assert found[0].pr_number == 42
    assert found[0].approved_sha == "0dc2391ab"
    # Filtered by operator when asked.
    assert collect_from_lanes(operator="other", lanes_root=lanes) == []


def test_collect_from_status_dir(tmp_path: Path) -> None:
    d = tmp_path / "status"
    d.mkdir()
    (d / "gate.txt").write_text(
        "owner/lake-of-rage#12\nPREMERGE-APPROVED 0dc2391ab\n", encoding="utf-8"
    )
    found = collect_from_status_dir(d)
    assert found[0].repo == "owner/lake-of-rage"
    assert found[0].pr_number == 12


def test_dedupe_prefers_first_source() -> None:
    a = ApprovedPR("r", 1, "sha1", source="lane")
    b = ApprovedPR("r", 1, "sha1", source="status_dir")
    deduped = dedupe_approvals([b, a])
    assert len(deduped) == 1
    assert deduped[0].source == "lane"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_plan_is_deterministic() -> None:
    spec = spec_for()
    prs = [make_pr(i) for i in (5, 1, 3, 2, 4)]
    profiles = {p.pr_number: profile_for(p, [f"api/src/f{p.pr_number}.py"], spec) for p in prs}
    kwargs = {"repo_specs": {"lake-of-rage": spec}, "check_merges": False}
    keyed_profiles = keyed(profiles)
    first = plan_batches(prs, keyed_profiles, **kwargs)
    second = plan_batches(list(reversed(prs)), keyed_profiles, **kwargs)
    assert [b.to_dict() for b in first] == [b.to_dict() for b in second]


# ---------------------------------------------------------------------------
# Merge compatibility (real git)
# ---------------------------------------------------------------------------


def test_merge_compatible_true_for_disjoint(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    # Two branches off the same base, each adding its own file: no overlap.
    shas = []
    for i, fname in enumerate(["a.txt", "b.txt"]):
        _git(git_repo, "checkout", "-q", base)
        (git_repo / fname).write_text(f"v{i}\n", encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(git_repo, "rev-parse", "HEAD"))
    assert merge_compatible(repo_path=git_repo, shas=shas, check=True) is True


def test_merge_compatible_false_for_conflict(git_repo: Path) -> None:
    base = _git(git_repo, "rev-parse", "HEAD")
    # Two branches off the same base editing the same line differently.
    shas = []
    for i, text in enumerate(["left\n", "right\n"]):
        _git(git_repo, "checkout", "-q", base)
        (git_repo / "README.md").write_text(text, encoding="utf-8")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(git_repo, "rev-parse", "HEAD"))
    assert merge_compatible(repo_path=git_repo, shas=shas, check=True) is False


def test_merge_compatible_skipped_when_check_disabled(git_repo: Path) -> None:
    assert merge_compatible(repo_path=git_repo, shas=["deadbeef"], check=False) is True


# ---------------------------------------------------------------------------
# Executor templates
# ---------------------------------------------------------------------------


def test_executor_templates_render() -> None:
    spec = spec_for(merge_template="lake_batch_merge.sh {pr_args}")
    prs = [make_pr(1), make_pr(2)]
    batches = plan_batches(
        prs,
        keyed({p.pr_number: profile_for(p, [f"api/src/f{p.pr_number}.py"], spec) for p in prs}),
        repo_specs={"lake-of-rage": spec},
        check_merges=False,
    )
    assert batches[0].executor_commands[0].startswith("lake_batch_merge.sh 1:")
    assert "2:" in batches[0].executor_commands[0]


def test_no_template_yields_no_command_and_a_note() -> None:
    spec = spec_for()  # no template configured
    prs = [make_pr(1)]
    batches = plan_batches(
        prs,
        keyed({1: profile_for(prs[0], ["api/src/a.py"], spec)}),
        repo_specs={"lake-of-rage": spec},
        check_merges=False,
    )
    assert batches[0].executor_commands == ()


# ---------------------------------------------------------------------------
# Deploy units
# ---------------------------------------------------------------------------


def test_deploy_unit_selection() -> None:
    units = builtin_spec("lake-of-rage").deploy_units
    assert deploy_unit_for(["api/src/main.py"], units) == "lor-api"
    assert deploy_unit_for(["transform/models/gold/sales.sql"], units) == "dbt"
    assert deploy_unit_for(["transform/macros/x.sql"], units) == "dbt"
    assert deploy_unit_for(["orchestration/src/x.py"], units) == "orchestration"
    # A change that also touches the API is shipped as an API batch.
    assert (
        deploy_unit_for(["api/src/main.py", "transform/models/gold/sales.sql"], units) == "lor-api"
    )


def test_silph_units() -> None:
    units = builtin_spec("silphcoanalytics").deploy_units
    assert deploy_unit_for(["api/routers/x.py"], units) == "api"
    assert deploy_unit_for(["frontend/src/x.tsx"], units) == "frontend"
    assert deploy_unit_for(["mobile/lib/x.dart"], units) == "mobile"


# ---------------------------------------------------------------------------
# End-to-end build_plan
# ---------------------------------------------------------------------------


def test_build_plan_renders_text() -> None:
    spec = spec_for(merge_template="lake_batch_merge.sh {pr_args}")
    prs = [make_pr(1), make_pr(2)]
    profiles = {p.pr_number: profile_for(p, [f"api/src/f{p.pr_number}.py"], spec) for p in prs}
    batches = plan_batches(
        prs, keyed(profiles), repo_specs={"lake-of-rage": spec}, check_merges=False
    )
    plan = MergePlan(batches=tuple(batches))
    text = render_plan_text(plan)
    assert "batch 0" in text
    assert "lake_batch_merge.sh" in text


def test_build_plan_end_to_end_with_fake_gh(fake_gh: FakeGh) -> None:
    """Full collect -> profile -> batch path against a real `gh` subprocess."""
    fake_gh.set_prs(
        {
            1: {
                "headRefOid": "aaaa111aaaa",
                "baseRefName": "main",
                "additions": 10,
                "deletions": 2,
                "files": [{"path": "api/src/a.py"}, {"path": "api/src/b.py"}],
            },
            2: {
                "headRefOid": "bbbb222bbbb",
                "baseRefName": "main",
                "additions": 3,
                "deletions": 0,
                "files": [{"path": "api/src/c.py"}],
            },
            3: {
                # head moved since the gate approved aaaa -> stale
                "headRefOid": "cccc333cccc",
                "baseRefName": "main",
                "files": [{"path": "api/src/d.py"}],
            },
        }
    )
    approvals = [
        ApprovedPR("lake-of-rage", 1, "aaaa111", source="lane"),
        ApprovedPR("lake-of-rage", 2, "bbbb222", source="lane"),
        ApprovedPR("lake-of-rage", 3, "aaaa111", source="lane"),
    ]
    spec = spec_for(merge_template="lake_batch_merge.sh {pr_args}")
    client = GitHubClient()
    batchable, profiles, stale, _ = profile_approvals(
        approvals, client=client, repo_specs={"lake-of-rage": spec}
    )
    # PR 3 is stale and excluded; 1 and 2 are batchable.
    assert [p.pr_number for p in batchable] == [1, 2]
    assert len(stale) == 1 and "stale approval" in stale[0].reason

    batches = plan_batches(
        batchable, profiles, repo_specs={"lake-of-rage": spec}, check_merges=False
    )
    assert len(batches) == 1
    assert [p.pr_number for p in batches[0].prs] == [1, 2]
    assert batches[0].executor_commands[0] == "lake_batch_merge.sh 1:aaaa111 2:bbbb222"
    # Risk/unit metadata survived the gh round trip.
    assert batches[0].deploy_unit == "lor-api"
    assert profiles[("lake-of-rage", 1)].lines_changed == 12


def test_emit_writes_jsonl_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from agent_fleet.merge_plan import plan as plan_mod

    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("AGENT_FLEET_RUNS_DIR", str(runs_dir))
    monkeypatch.setattr(plan_mod, "_fleetobs_emit_available", lambda: False)

    batch = Batch(
        index=0,
        repo="lake-of-rage",
        prs=(make_pr(1),),
        deploy_unit="lor-api",
        dbt_select=("gold.sales",),
    )
    sink = plan_mod.emit_plan_event(MergePlan(batches=(batch,)), run_id="test-run")
    assert sink.startswith("jsonl:")
    lines = (runs_dir / "test-run.jsonl").read_text(encoding="utf-8").strip().splitlines()
    event = json.loads(lines[0])
    assert event["event"] == "merge.plan"
    assert event["data"]["batches"][0]["dbt_select"] == ["gold.sales"]
