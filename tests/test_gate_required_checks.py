"""Per-repo REQUIRED CHECKS in the gate (contract + correctness).

The gate's deterministic half was pytest, and pytest only. A lake-of-rage PR
reviewed clean and merged while breaking dbt unit-test compilation, which took
out four production rebuild jobs afterwards: nothing in the gate knew that repo
had a second kind of build. ``gate.required_checks`` is the fix, and these tests
pin the two properties that make it safe.

The first is that a check result is never read as more than it is. A non-zero
exit is a confirmed blocker with the command's own output as evidence, exactly
like a failing test in step0. An unrunnable check — missing command, timeout,
unparseable argv — is *not* a finding about the code, and fails the run closed
instead. Collapsing those two is the exact bug this feature could reintroduce,
so the distinction is asserted directly rather than through an outcome field.

The second is that a check only runs when the diff touches a path it names. A
check that ignores its own selector is a check every PR pays for, and one that
runs on a docs diff tells the operator nothing about the docs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import TYPE_CHECKING, Any, cast

import pytest

from agent_fleet.gate import checks as gate_checks
from agent_fleet.gate.checks import (
    DEFAULT_CHECK_MEMORY,
    DEFAULT_CHECK_TIMEOUT_S,
    MATCH_ALL,
    RequiredCheck,
    changed_model_names,
    run_check,
    run_checks,
    select_checks,
)
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.metrics import GateMetrics, summarize_rows
from agent_fleet.gate.pipeline import TestRun

if TYPE_CHECKING:
    from agent_fleet.hooks import LLMBackend

# ---------------------------------------------------------------------------
# Fixtures: a real git repo, and fake commands instead of real tooling
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one base commit and a ``pr`` branch touching a dbt model.

    Real git, because the pipeline selects checks from ``git diff`` against the
    base branch: a fake change list would test the selector and not the thing
    that actually feeds it.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _git(root, "checkout", "-q", "-b", "pr")
    model = root / "transform" / "models" / "stg_orders"
    model.mkdir(parents=True)
    (model / "stg_orders.sql").write_text("select 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr change")
    return root


def _cfg(raw: dict[str, Any] | None) -> GateConfig:
    """``load_gate_config(raw)`` asserted non-None, typed so tests can read fields.

    Every test here configures a gate and then reads a field off it, and the
    loader returns ``None`` for a disabled gate. Binding it once keeps the
    assertion on every test instead of a ``# type: ignore`` on every read.
    """
    cfg = load_gate_config(raw)
    assert cfg is not None, "gate must not be disabled by this config"
    return cfg


def _check(
    name: str,
    command: str,
    *,
    when_paths: tuple[str, ...] = MATCH_ALL,
    timeout_s: int = DEFAULT_CHECK_TIMEOUT_S,
    memory: str = DEFAULT_CHECK_MEMORY,
) -> RequiredCheck:
    return RequiredCheck(
        name=name,
        command=command,
        when_paths=when_paths,
        timeout_s=timeout_s,
        memory=memory,
    )


def _py(code: str) -> str:
    """A command string that runs *code* with the interpreter running the tests.

    Every check in this file is a fake, so the only real dependency a test needs
    is a program that can print and choose an exit code. Building it through one
    helper keeps the escaped quoting in a single place and lets each test read as
    the program it wants to run rather than as string plumbing.
    """
    return f'{sys.executable} -c "{code}"'


# ---------------------------------------------------------------------------
# changed_models
# ---------------------------------------------------------------------------


def test_changed_models_reads_dbt_model_names_from_the_models_layout() -> None:
    names = changed_model_names(
        [
            "transform/models/stg_orders/stg_orders.sql",
            "transform/models/int_orders/int_orders.py",
            "transform/models/marts/daily/daily.sql",
        ]
    )
    assert names == ["daily", "int_orders", "stg_orders"]


def test_changed_models_ignores_paths_that_are_not_models() -> None:
    """A directory merely *near* ``models/`` must not contribute a model name.

    ``{changed_models}`` is documented as a list of model names. Letting a
    non-model path contribute its filename would make a check that shells out
    with that placeholder run against names that do not exist.
    """
    assert (
        changed_model_names(
            [
                "src/models_helper.py",
                "transform/models/README.md",
                "docs/models.md",
                "transform/snapshots/orders.sql",
            ]
        )
        == []
    )


# ---------------------------------------------------------------------------
# Selection by path
# ---------------------------------------------------------------------------


def test_check_runs_only_when_a_changed_path_matches() -> None:
    check = _check("dbt-compile", "true", when_paths=(r"^transform/",))
    assert select_checks([check], ["transform/models/stg_orders/stg_orders.sql"]) == [check]
    assert select_checks([check], ["README.md"]) == []


def test_unscoped_check_matches_every_diff() -> None:
    """A check with no ``when_paths`` is match-all.

    A repo that lists a check without scoping it meant "always"; defaulting to
    match-nothing would let a half-written config quietly stop being enforced,
    which is the failure mode this feature exists to prevent.
    """
    check = _check("always", "true")
    assert select_checks([check], ["README.md"]) == [check]


def test_empty_diff_selects_nothing_even_for_an_unscoped_check() -> None:
    """An empty change list must not run the whole set.

    An empty diff is just as likely to be a failed ``git diff`` as a PR that
    touched nothing, and running every check against it would report red checks
    for code the PR never touched.
    """
    check = _check("always", "true")
    assert select_checks([check], []) == []


def test_select_checks_preserves_configured_order() -> None:
    checks = [
        _check("first", "true", when_paths=(".",)),
        _check("second", "true", when_paths=(".",)),
    ]
    assert [c.name for c in select_checks(checks, ["a.py"])] == ["first", "second"]


# ---------------------------------------------------------------------------
# Execution: the four outcomes, against a real worktree
# ---------------------------------------------------------------------------


def test_passing_check_reports_passed_and_no_evidence(repo: Path) -> None:
    result = run_check(
        _check("ok", _py("print('fine')")),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
    )
    assert result.passed
    assert not result.could_not_run
    assert result.returncode == 0
    assert "fine" in result.evidence


def test_failing_check_is_a_blocker_carrying_the_command_tail(repo: Path) -> None:
    """A non-zero exit is a finding, and the evidence is the command's own words.

    The tail is what the fixer is handed, so it has to be the real output rather
    than a generic "command failed": the whole point of the check is that the
    failure text says what broke.
    """
    result = run_check(
        _check(
            "dbt-compile",
            _py("import sys; print('Compilation Error in model x'); sys.exit(2)"),
        ),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
    )
    assert not result.passed
    assert not result.could_not_run
    assert result.returncode == 2
    assert "Compilation Error in model x" in result.evidence


def test_missing_command_is_could_not_run_not_a_failure(repo: Path) -> None:
    """A binary that does not exist is an infra failure, not a red build.

    This is the distinction the whole design turns on. Reporting it as a failed
    check would send a fixer to repair code that is fine; approving it would let
    a repo whose toolchain is missing merge unchecked. It is neither — it fails
    the run closed.
    """
    result = run_check(
        _check("dbt-compile", "definitely-not-a-real-binary-xyz"),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
    )
    assert not result.passed
    assert result.could_not_run
    assert result.returncode == 127
    assert "could not run" in result.reason


def test_timeout_is_could_not_run_not_a_failure(repo: Path) -> None:
    """A hung check is not evidence the code is broken."""
    result = run_check(
        _check("hangs", _py("import time; time.sleep(30)"), timeout_s=1),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
    )
    assert not result.passed
    assert result.could_not_run
    assert "timed out" in result.reason


def test_unparseable_command_is_could_not_run(repo: Path) -> None:
    """A quoting bug in a repo's config is the config's problem, not the PR's.

    Written by hand rather than through :func:`_py` because the unbalanced quote
    *is* the input under test — a helper that always balanced its quotes could
    not produce this case.
    """
    result = run_check(
        _check("typo", f'{sys.executable} -c "print(1'),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
    )
    assert not result.passed
    assert result.could_not_run
    assert "could not be parsed" in result.reason


def test_check_runs_in_the_worktree_not_the_callers_cwd(repo: Path) -> None:
    """The check must see the tree under review.

    A check that ran in the gate process's cwd would read whatever happened to
    be checked out there — for a merged-tree recheck, the wrong tree entirely.
    """
    (repo / "marker.txt").write_text("present\n", encoding="utf-8")
    result = run_check(
        _check("sees-tree", _py("open('marker.txt').close(); print('ok')")),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
    )
    assert result.passed


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------


def test_changed_files_and_models_placeholders_are_substituted(repo: Path) -> None:
    """The two documented placeholders reach the command's argv.

    The program echoes ``sys.argv`` rather than printing a literal, because the
    substitution happens in the command *string*: writing the placeholder inside
    the Python source would have the gate substitute it there too, and the test
    would then be asserting that Python prints braces rather than that the right
    values were substituted.
    """
    result = run_check(
        _check(
            "echo",
            _py("import sys; print('|'.join(sys.argv[1:]))")
            + " {changed_files} -- {changed_models}",
        ),
        worktree=repo,
        stage="head",
        changed_files=["transform/models/stg_orders/stg_orders.sql", "README.md"],
        changed_models=["stg_orders"],
    )
    assert result.passed
    # Each substituted list arrives as its own argv entries, joined by the
    # program's own separator — which is what "space-joined, quoted by the
    # author" means in practice.
    assert "transform/models/stg_orders/stg_orders.sql" in result.evidence
    assert "README.md" in result.evidence
    assert "stg_orders" in result.evidence
    # Every placeholder was replaced: no braces survive into the command.
    assert "{" not in result.evidence


def test_brace_expansion_in_a_command_is_left_alone(repo: Path) -> None:
    """Only the two documented placeholders are substituted.

    A shell brace expansion in a repo's own command is the author's business. A
    blanket ``str.format`` would raise on it, and a blanket regex would quietly
    rewrite it into something the author did not write.
    """
    result = run_check(
        _check("braces", _py("print('{{1,2}}')")),
        worktree=repo,
        stage="head",
        changed_files=[],
        changed_models=[],
    )
    assert result.passed
    assert "{1,2}" in result.evidence


# ---------------------------------------------------------------------------
# run_checks over a real diff
# ---------------------------------------------------------------------------


def test_run_checks_selects_from_the_real_diff_and_records_both(repo: Path) -> None:
    """End to end over a real diff: a matching check runs, a non-matching one does not."""
    checks = [
        _check("dbt-compile", _py("print('compiled')"), when_paths=(r"^transform/",)),
        _check("docs-lint", _py("import sys; sys.exit(3)"), when_paths=(r"^docs/",)),
    ]
    results = run_checks(
        checks,
        worktree=repo,
        stage="head",
        changed_files=["transform/models/stg_orders/stg_orders.sql"],
    )
    assert [r.name for r in results] == ["dbt-compile"]
    assert results[0].passed
    assert results[0].stage == "head"


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_required_checks_parse_every_documented_field() -> None:
    cfg = _cfg(
        {
            "gate": {
                "required_checks": [
                    {
                        "name": "dbt-compile",
                        "command": "dbt compile",
                        "when_paths": [r"^transform/", r"^macros/"],
                        "timeout_s": 600,
                        "memory": "4G",
                    }
                ]
            }
        }
    )
    (check,) = cfg.required_checks
    assert check.name == "dbt-compile"
    assert check.command == "dbt compile"
    assert check.when_paths == (r"^transform/", r"^macros/")
    assert check.timeout_s == 600
    assert check.memory == "4G"


def test_required_checks_default_to_empty_and_apply_no_defaults() -> None:
    """Absent means no checks, not "some default checks".

    Turning the gate on at a repo must not change its behaviour until someone
    has said what that repo's build is.
    """
    assert _cfg({}).required_checks == ()
    assert _cfg({"gate": {}}).required_checks == ()


def test_required_check_without_a_command_is_dropped_not_kept() -> None:
    """An entry that cannot run is dropped with a warning, never half-enforced.

    Keeping it would fail every PR closed on a typo in the config, which is the
    shape of "the gate is broken" rather than "this PR is broken".
    """
    cfg = _cfg({"gate": {"required_checks": [{"name": "no-command"}, {"command": "true"}, "junk"]}})
    assert cfg.required_checks == ()


def test_required_check_with_a_nonpositive_timeout_falls_back_to_the_default() -> None:
    """``timeout_s: 0`` would kill every check instantly and read as infra failure."""
    cfg = _cfg({"gate": {"required_checks": [{"name": "x", "command": "true", "timeout_s": 0}]}})
    (check,) = cfg.required_checks
    assert check.timeout_s == DEFAULT_CHECK_TIMEOUT_S


def test_required_check_with_an_unusable_timeout_falls_back_to_the_default() -> None:
    cfg = _cfg(
        {"gate": {"required_checks": [{"name": "x", "command": "true", "timeout_s": "soon"}]}}
    )
    (check,) = cfg.required_checks
    assert check.timeout_s == DEFAULT_CHECK_TIMEOUT_S


def test_empty_when_paths_list_is_match_all_not_match_none() -> None:
    cfg = _cfg({"gate": {"required_checks": [{"name": "x", "command": "true", "when_paths": []}]}})
    (check,) = cfg.required_checks
    assert check.matches(["README.md"])


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_row_carries_check_results() -> None:
    """A run that merged on a red check must be distinguishable afterwards."""
    metric = GateMetrics(
        run_id="r1",
        repo="lake",
        pr=7,
        start_sha="a" * 40,
        outcome="converged",
        checks=[
            {"name": "dbt-compile", "stage": "head", "passed": False, "could_not_run": False},
            {"name": "lint", "stage": "merged", "passed": True, "could_not_run": False},
        ],
    )
    row: dict[str, Any] = dict(metric.to_dict())
    carried: list[dict[str, Any]] = list(row["checks"])
    assert [str(c["name"]) for c in carried] == ["dbt-compile", "lint"]


def test_summary_counts_failed_and_unrunnable_checks_separately() -> None:
    """The two failure modes are counted apart, or the summary hides one.

    Collapsing them would make an infra failure look like an ordinary red check
    in the one place an operator goes to ask "how often are our checks firing".
    """
    rows: list[dict[str, Any]] = [
        {
            "outcome": "converged",
            "checks": [
                {"name": "a", "passed": True, "could_not_run": False},
                {"name": "b", "passed": False, "could_not_run": False},
                {"name": "c", "passed": False, "could_not_run": True},
            ],
        }
    ]
    summary = summarize_rows(rows)
    assert summary["check_outcomes"] == {"passed": 1, "failed": 1, "could-not-run": 1}


def test_summary_tolerates_a_missing_or_malformed_checks_column() -> None:
    """Rows written before this column existed must still summarise."""
    assert summarize_rows([{"outcome": "converged"}])["check_outcomes"] == {
        "passed": 0,
        "failed": 0,
        "could-not-run": 0,
    }
    assert summarize_rows([{"outcome": "converged", "checks": "nope"}])["check_outcomes"] == {
        "passed": 0,
        "failed": 0,
        "could-not-run": 0,
    }


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------


class _NoBackend:
    """A backend that refuses if anything tries to dispatch an agent.

    Not a silent no-op: a stub that returned an empty answer would let a wiring
    test pass for the wrong reason, by looking exactly like a reviewer that
    found no blockers. Raising makes "the check stage must not need a model" a
    property the test enforces rather than one it assumes.
    """

    def run(self, *args: object, **kwargs: object) -> Any:  # noqa: ANN401, ARG002
        raise AssertionError("the required-check stage must not dispatch an agent")


def _pipeline(repo: Path, gate_dir: Path, checks: list[dict[str, Any]]) -> Any:  # noqa: ANN401
    """A GatePipeline with a backend that must never be called."""
    from agent_fleet.gate.pipeline import GatePipeline
    from agent_fleet.model_policy import parse_model_policy

    return GatePipeline(
        repo=repo,
        pr_number=1,
        config=_cfg({"gate": {"required_checks": checks}}),
        policy=parse_model_policy({}),
        backend=cast("LLMBackend", _NoBackend()),
        gate_dir=gate_dir,
        use_systemd=False,
    )


def test_failing_check_becomes_a_confirmed_blocker_with_evidence(
    repo: Path, tmp_path: Path
) -> None:
    """The gate hands a red check to the fixer exactly as it hands a red test."""
    pipeline = _pipeline(
        repo,
        tmp_path / "gate",
        [
            {
                "name": "dbt-compile",
                "command": _py("print('Error in model stg_orders'); sys.exit(1)"),
                "when_paths": [r"^transform/"],
            }
        ],
    )
    results = pipeline.run_required_checks(repo, stage="head")

    assert len(results) == 1
    assert not results[0].passed
    blockers = [c for c in pipeline.evidence.confirmed if c.get("source") == "required-check"]
    assert len(blockers) == 1
    assert "dbt-compile" in blockers[0]["claim"]
    assert "Error in model stg_orders" in blockers[0]["evidence"]


def test_unrunnable_check_raises_infra_error_rather_than_approving(
    repo: Path, tmp_path: Path
) -> None:
    """A check that could not run fails the run closed.

    It must not be recorded as a confirmed blocker either: the fixer would be
    dispatched to repair a working codebase because a binary is missing.
    """
    from agent_fleet.gate.pipeline import GateInfraError

    pipeline = _pipeline(
        repo, tmp_path / "gate", [{"name": "gone", "command": "definitely-not-a-real-binary-xyz"}]
    )
    with pytest.raises(GateInfraError) as excinfo:
        pipeline.run_required_checks(repo, stage="head")

    assert "could not run" in str(excinfo.value)
    assert not [c for c in pipeline.evidence.confirmed if c.get("source") == "required-check"]
    # The result is still recorded, so the metrics row explains the escalation.
    assert [r.name for r in pipeline._check_results] == ["gone"]
    assert pipeline._check_results[0].could_not_run


def test_no_configured_checks_runs_nothing(repo: Path, tmp_path: Path) -> None:
    """A repo with no ``required_checks`` is unaffected by this feature."""
    pipeline = _pipeline(repo, tmp_path / "gate", [])
    assert pipeline.run_required_checks(repo) == []
    assert pipeline._check_results == []


def test_non_matching_check_does_not_run_against_the_diff(repo: Path, tmp_path: Path) -> None:
    """The docs path in a model diff must not trigger the dbt compile."""
    pipeline = _pipeline(
        repo,
        tmp_path / "gate",
        [
            {
                "name": "dbt-compile",
                # Would fail loudly if it ever ran.
                "command": _py("import sys; sys.exit(9)"),
                "when_paths": [r"^docs/"],
            }
        ],
    )
    assert pipeline.run_required_checks(repo) == []
    assert pipeline._check_results == []


def test_check_sees_the_merged_tree_not_the_pr_tree(repo: Path, tmp_path: Path) -> None:
    """A check that is green on the PR tree and red once base is merged must fail.

    This is the merged-tree regression the second run exists to catch, and it is
    the case a head-only run would miss: the check passes in ``run()``, the base
    moves, the combined tree stops compiling, and nothing looks again. The fake
    check decides by reading a file only the merge produces, so "green at head,
    red merged" is a property of the fixture rather than of the wiring.
    """
    from agent_fleet.gate.gitops import merge_base_into, prepare_worktree, resolve_diff_base

    # main lands a change the PR branch has never seen. This has to happen
    # before the merged worktree is built, or the merge could not pick it up and
    # the fixture would report green on both trees for the wrong reason.
    _git(repo, "checkout", "-q", "main")
    (repo / "base-only.txt").write_text("landed on main\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main change")
    # Back to the PR branch: the head-stage run below measures *its* diff, and a
    # repo left on main would silently select nothing instead.
    _git(repo, "checkout", "-q", "pr")

    merged = tmp_path / "merged-wt"
    prepare_worktree(repo, merged, "pr")
    merge_base_into(merged, resolve_diff_base(repo, "main"))

    # The head stage gets its own worktree at the PR head, exactly as `run()`
    # does. Running it in the repo checkout would measure the branch's working
    # tree rather than the commit under review.
    head_wt = tmp_path / "head-wt"
    prepare_worktree(repo, head_wt, "pr")

    pipeline = _pipeline(
        repo,
        tmp_path / "gate",
        [
            {
                "name": "needs-base-file",
                # Green on the PR tree, which has never seen the base commit;
                # red once the base is merged in and the file appears. The check
                # models a build that only breaks on the combined tree.
                "command": _py(
                    "import os, sys; sys.exit(1 if os.path.exists('base-only.txt') else 0)"
                ),
                "when_paths": [r"^transform/"],
            }
        ],
    )

    at_head = pipeline.run_required_checks(head_wt, stage="head")
    assert [r.passed for r in at_head] == [True], "fixture must be green on the PR tree"

    at_merged = pipeline.run_required_checks(merged, stage="merged")
    assert [r.passed for r in at_merged] == [False]
    assert at_merged[0].stage == "merged"
    # The blocker names *where* it failed, so a merged-tree regression cannot be
    # read as a failure of the PR's own head.
    assert "merged with base" in gate_checks.blocker_claim(at_merged[0])


def test_recheck_runs_the_checks_on_the_merged_tree(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recheck must actually invoke the check stage, not merely support it.

    ``run_required_checks(stage="merged")`` is a capability; this pins the *call*
    that uses it. A test that only exercised the method directly would keep
    passing if the invocation in :func:`run_gate_recheck` were deleted — which is
    the regression that matters, since that call is the only path from a recheck
    to the check stage. Asserting on the recorded call keeps this independent of
    whether the recheck ultimately approves.
    """
    import agent_fleet.gate.pipeline as pipeline_mod

    seen: list[str] = []

    def _record(self: Any, worktree: Path, *, stage: str = "head") -> Any:  # noqa: ANN401, ARG001
        seen.append(stage)
        return []

    monkeypatch.setattr(pipeline_mod.GatePipeline, "run_required_checks", _record)
    # The recheck's own test run is irrelevant here and is the slow part; the
    # subject is the check call, so it is stubbed to a green no-op.
    monkeypatch.setattr(
        pipeline_mod.GateTestRunner,
        "run",
        lambda self, test_files: TestRun(),  # noqa: ARG005
    )

    pipeline_mod.run_gate_recheck(
        repo_path=repo,
        pr_number=1,
        approved_sha="deadbeef",
        head_sha="pr",
        gate_dir=tmp_path / "gate",
    )

    assert seen == ["merged"], f"recheck did not run the checks; stages seen: {seen}"
