"""The STANDARD bar: risk-matched review and its pass/fallback state machine.

The full evidence gate spends four reviewers, a verifier per claim, a judge and a
convergence loop on every PR. For a well-scoped change to ordinary product code
that is the wrong price. The STANDARD bar reviews such a diff once, with one
all-focus reviewer, and leans harder on the deterministic half (the PR's own
tests must be green) to pay for the cheaper model side.

The rules that decide whether a head is approved, re-gated, or handed to the
full gate are pure functions, so these tests exercise them directly — no repo, no
network, no model. They pin the *refusals* (never approve with a blocker, never
loop past the budget) rather than the log wording, because an approval that
fires one time too early is the bug worth writing a test for.

Two things make the bar safe rather than merely cheap, and both are tested here:
a sensitive path is a veto (the full pipeline keeps running), and the cheap bar
is finite (a pass counter that survives the process, because the gate exits
after one run).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any, Protocol

import pytest

from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import GatePipeline, PullRequestRef
from agent_fleet.gate.standard import (
    OUTCOME_APPROVED,
    OUTCOME_FALLBACK,
    OUTCOME_FIX_AND_REGATE,
    SENSITIVE_TIER,
    STANDARD_TIER,
    FallbackReason,
    StandardAction,
    StandardState,
    next_action,
    prior_passes,
    select_tier,
)
from agent_fleet.model_policy import ModelPolicy

# ---------------------------------------------------------------------------
# Fakes — no network, no real model, no real git
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """Replays one answer per call and records the prompts it was given."""

    answer: str = json.dumps({"findings": []})
    prompts: list[str] = field(default_factory=list)

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        return type(
            "R", (), {"exit_code": 0, "stdout": self.answer, "stderr": "", "duration_s": 0.0}
        )()


def _config(**overrides: object) -> GateConfig:
    base: dict[str, object] = {
        "backend": "cmd",
        "model": "m",
        "judge_backend": "cmd",
        "judge_model": "m",
        "enable_judge": False,
        "enable_fix": True,
    }
    base.update(overrides)
    return GateConfig(**base)  # type: ignore[arg-type]


def _ref() -> PullRequestRef:
    return PullRequestRef(
        number=42, head_ref="fb/lane", head_sha="a" * 40, state="OPEN", base_ref="main"
    )


def _pipeline(tmp_path: Path, backend: _FakeBackend, config: GateConfig) -> GatePipeline:
    return GatePipeline(
        repo=tmp_path / "agent-fleet",
        pr_number=42,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


class _DiffSetter(Protocol):
    """``(changed paths) -> None``: a test states its diff rather than building one."""

    def __call__(self, paths: list[str]) -> None: ...


@pytest.fixture
def diff(monkeypatch: pytest.MonkeyPatch) -> _DiffSetter:
    """Stub the git read tier selection depends on, so no repository is needed."""

    def _set(paths: list[str]) -> None:
        for module in ("agent_fleet.gate.pipeline", "agent_fleet.gate.gitops"):
            monkeypatch.setattr(f"{module}.changed_paths", lambda *_a, **_k: list(paths))
            monkeypatch.setattr(f"{module}.diff_line_stats", lambda *_a, **_k: 0)
            monkeypatch.setattr(f"{module}.prodsensitive_paths", lambda *_a, **_k: [])

    return _set


@pytest.fixture(autouse=True)
def _no_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """No metrics from the operator's real box leak into a pass-counter decision."""
    monkeypatch.setattr("agent_fleet.gate.metrics.read_metrics", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "agent_fleet.gate.metrics.GateMetrics.append_metrics",
        lambda _self, *_a, **_k: None,
    )


# ---------------------------------------------------------------------------
# Tier selection
# ---------------------------------------------------------------------------


def test_an_ordinary_product_diff_earns_the_standard_bar() -> None:
    assert select_tier(_config(), ["agent_fleet/foo.py", "tests/test_foo.py"]) == STANDARD_TIER


def test_an_empty_diff_earns_the_standard_bar() -> None:
    """No changed file is not evidence of risk; the run's other guards still apply."""
    assert select_tier(_config(), []) == STANDARD_TIER


@pytest.mark.parametrize(
    "path",
    [
        "dags/gold/daily_sales.py",
        "agent_fleet/sales/revenue.py",
        "db/identity/users.sql",
        "ops/stamp_orders.py",
        "db/migrations/0001_x.sql",
        "db/migration/0001_x.sql",
        "api/schema_view.py",
        ".github/workflows/ci.yml",
        "infra/vps/deploy.sh",
        "scripts/deploy_prod.sh",
        "ops/run_prod.py",
    ],
)
def test_a_sensitive_path_is_a_veto(path: str) -> None:
    """One sensitive path anywhere in the diff forces the full evidence pipeline."""
    assert select_tier(_config(), [path]) == SENSITIVE_TIER
    assert select_tier(_config(), ["agent_fleet/foo.py", path]) == SENSITIVE_TIER


def test_the_sensitive_list_is_configurable() -> None:
    config = _config(sensitive_paths=(r"^only_here/",))
    assert select_tier(config, ["only_here/x.py"]) == SENSITIVE_TIER
    assert select_tier(config, ["dags/gold/daily_sales.py"]) == STANDARD_TIER


def test_an_empty_sensitive_list_is_honoured() -> None:
    """An empty list means "nothing is sensitive here", not "use the defaults"."""
    config = _config(sensitive_paths=())
    assert select_tier(config, ["dags/gold/daily_sales.py"]) == STANDARD_TIER


def test_is_sensitive_matches_the_configured_patterns() -> None:
    config = _config()
    assert config.is_sensitive("db/migrations/1.sql")
    assert not config.is_sensitive("agent_fleet/foo.py")


# ---------------------------------------------------------------------------
# The state machine: the four rules
# ---------------------------------------------------------------------------


def test_zero_findings_and_green_tests_approve() -> None:
    decision = next_action(StandardState(findings=0, pr_tests_failed=False))
    assert decision.action is StandardAction.APPROVE
    assert decision.passes == 0
    assert decision.fallback_reason is None


def test_a_reviewer_finding_spends_one_pass() -> None:
    decision = next_action(StandardState(findings=2, pr_tests_failed=False))
    assert decision.action is StandardAction.FIX_AND_REGATE
    assert decision.passes == 1
    assert decision.fallback_reason is None


def test_a_red_pr_test_alone_spends_a_pass() -> None:
    """step0 is a blocker on its own: the reviewer can find nothing and the PR
    is still not clean, so a pass is spent."""
    decision = next_action(StandardState(findings=0, pr_tests_failed=True))
    assert decision.action is StandardAction.FIX_AND_REGATE
    assert decision.passes == 1


def test_an_approval_is_never_charged_a_pass() -> None:
    """A PR that needed no fixer must not consume the budget."""
    decision = next_action(StandardState(findings=0, pr_tests_failed=False, passes=2))
    assert decision.action is StandardAction.APPROVE
    assert decision.passes == 2  # the count it already had, not a new spend


def test_a_fixer_that_changes_nothing_falls_back() -> None:
    """Every finding disputed and no diff is the fixer declining the work, not fixing it."""
    decision = next_action(
        StandardState(findings=3, pr_tests_failed=False, fixer_changed_nothing=True, passes=1)
    )
    assert decision.action is StandardAction.FALLBACK
    assert decision.fallback_reason is FallbackReason.DISPUTED


@pytest.mark.parametrize("passes", [3, 4, 9])
def test_the_pass_budget_falls_back(passes: int) -> None:
    decision = next_action(StandardState(findings=1, pr_tests_failed=True, passes=passes))
    assert decision.action is StandardAction.FALLBACK
    assert decision.fallback_reason is FallbackReason.PASS_BUDGET


def test_the_default_budget_is_three() -> None:
    assert StandardState().max_passes == 3
    # pass 1 and 2 still fix; pass 3 (the third) is the one that falls back.
    assert next_action(StandardState(findings=1, passes=2)).action is StandardAction.FIX_AND_REGATE
    assert next_action(StandardState(findings=1, passes=3)).action is StandardAction.FALLBACK


def test_a_raised_budget_buys_more_passes() -> None:
    state = StandardState(findings=1, passes=4, max_passes=5)
    assert next_action(state).action is StandardAction.FIX_AND_REGATE


def test_disputed_beats_the_budget_check() -> None:
    """When a fixer changed nothing we name *that*, not the generic budget reason."""
    decision = next_action(
        StandardState(findings=1, passes=3, fixer_changed_nothing=True)
    )
    assert decision.fallback_reason is FallbackReason.DISPUTED


def test_every_decision_carries_a_readable_summary() -> None:
    for state in (
        StandardState(head="abcdef1234"),
        StandardState(findings=1, head="abcdef1234"),
        StandardState(findings=1, passes=3),
    ):
        assert next_action(state).summary


# ---------------------------------------------------------------------------
# The durable pass counter
# ---------------------------------------------------------------------------


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "repo": "agent-fleet",
        "pr": 42,
        "tier": STANDARD_TIER,
        "outcome": OUTCOME_FIX_AND_REGATE,
    }
    base.update(overrides)
    return base


def test_prior_passes_counts_this_prs_standard_passes() -> None:
    rows = [_row(), _row()]
    assert prior_passes(rows, repo="agent-fleet", pr=42) == 2


def test_prior_passes_ignores_other_prs_and_repos() -> None:
    rows = [_row(pr=7), _row(repo="other"), _row()]
    assert prior_passes(rows, repo="agent-fleet", pr=42) == 1


def test_prior_passes_ignores_full_gate_rows() -> None:
    """A full-gate run for the same PR is not a STANDARD pass."""
    rows = [_row(tier="", outcome="converged"), _row()]
    assert prior_passes(rows, repo="agent-fleet", pr=42) == 1


def test_prior_passes_stops_at_a_fallback() -> None:
    """Once a head was handed to the full gate, the cheap-bar budget is spent."""
    rows = [_row(), _row(), _row(outcome=OUTCOME_FALLBACK)]
    assert prior_passes(rows, repo="agent-fleet", pr=42) == 0


def test_prior_passes_does_not_count_a_standard_approval() -> None:
    rows = [_row(), _row(outcome=OUTCOME_APPROVED)]
    # The approval is the newest row and is not a fix pass; the earlier fix counts.
    assert prior_passes(rows, repo="agent-fleet", pr=42) == 1


def test_prior_passes_on_no_history_is_zero() -> None:
    assert prior_passes([], repo="agent-fleet", pr=42) == 0


def test_prior_passes_tolerates_junk_values() -> None:
    """Metrics come off disk; bad input must read as zero, not raise."""
    rows = [{"repo": "agent-fleet", "pr": "not-a-number", "tier": STANDARD_TIER}]
    assert prior_passes(rows, repo="agent-fleet", pr=42) == 0


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_the_sensitive_defaults_exist() -> None:
    config = load_gate_config({})
    assert config is not None
    assert config.sensitive_paths
    assert config.standard_max_passes == 3
    assert config.is_sensitive("dags/gold/x.py")
    assert not config.is_sensitive("agent_fleet/foo.py")


def test_the_sensitive_keys_load() -> None:
    config = load_gate_config({"gate": {"sensitive_paths": ["^only/"], "standard_max_passes": 5}})
    assert config is not None
    assert config.sensitive_paths == ("^only/",)
    assert config.standard_max_passes == 5


def test_a_malformed_sensitive_list_falls_back_to_the_defaults() -> None:
    config = load_gate_config({"gate": {"sensitive_paths": "^oops"}})
    assert config is not None
    assert config.sensitive_paths == GateConfig().sensitive_paths


# ---------------------------------------------------------------------------
# The branch in run(): which bar a diff actually reaches
# ---------------------------------------------------------------------------


def test_a_clean_diff_runs_one_all_focus_reviewer_and_nothing_else(
    tmp_path: Path, diff: _DiffSetter
) -> None:
    """The cheap bar is one reviewer: no parallel lenses, no verifier, no judge."""
    diff(["agent_fleet/foo.py"])
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, _config())
    pipe.evidence.confirmed.clear()

    result = pipe.run_standard(_ref(), tmp_path / "wt", [])

    assert len(backend.prompts) == 1
    assert result.metrics is not None
    assert result.metrics.tier == STANDARD_TIER
    assert result.outcome.value == "APPROVED"  # zero findings, tests green
    assert result.metrics.outcome == OUTCOME_APPROVED
    assert result.metrics.passes == 0
    assert result.approved


def test_a_reported_finding_dispatches_one_fixer_and_ends_as_re_gate(
    tmp_path: Path, diff: _DiffSetter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blockers are the reviewer's findings, and the run ends 're-gate new head'."""
    diff(["agent_fleet/foo.py"])
    answer = json.dumps(
        {
            "findings": [
                {
                    "id": "F1",
                    "file": "a.py",
                    "line": 3,
                    "claim": "off by one",
                    "repro": "x",
                    "testable": True,
                }
            ]
        }
    )
    backend = _FakeBackend(answer=answer)
    pipe = _pipeline(tmp_path, backend, _config())
    prompts = backend.prompts
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GatePipeline._standard_fixer",
        lambda _self, *_a, **_k: prompts.append("fixer"),
    )
    # The fixer pushed a new head, so this run re-gates rather than falling back.
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.current_pr_head", lambda *_a, **_k: "b" * 40
    )

    result = pipe.run_standard(_ref(), tmp_path / "wt", [])

    assert "fixer" in prompts  # exactly one fixer
    assert not result.approved
    assert result.metrics is not None
    assert result.metrics.outcome == OUTCOME_FIX_AND_REGATE
    assert result.metrics.passes == 1
    assert "re-gate new head" in result.reasons[0]
    assert "PREMERGE-APPROVED" not in result.status_line


def test_a_fixer_that_pushed_nothing_falls_back_as_disputed(
    tmp_path: Path, diff: _DiffSetter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixer that disputes every finding and moves no head must not buy a pass.

    The head is re-read after the fixer, so this is decided on the run that
    dispatched it: spending a pass on a fixer that declined the work is how the
    cheap bar becomes a treadmill.
    """
    diff(["agent_fleet/foo.py"])
    answer = json.dumps(
        {
            "findings": [
                {
                    "id": "F1",
                    "file": "a.py",
                    "line": 3,
                    "claim": "off by one",
                    "repro": "x",
                    "testable": True,
                }
            ]
        }
    )
    backend = _FakeBackend(answer=answer)
    pipe = _pipeline(tmp_path, backend, _config())
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GatePipeline._standard_fixer", lambda *_a, **_k: None
    )
    monkeypatch.setattr("agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None)
    # The head never moved: the fixer changed nothing.
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.current_pr_head", lambda *_a, **_k: "a" * 40
    )

    result = pipe.run_standard(_ref(), tmp_path / "wt", [])

    assert not result.approved
    assert result.metrics is not None
    assert result.metrics.outcome == OUTCOME_FALLBACK
    assert "full evidence gate required" in result.reasons[0]
    assert "disputed" in result.reasons[0]
    assert "PREMERGE-APPROVED" not in result.status_line


def test_a_spent_budget_ends_as_fallback_to_the_full_gate(
    tmp_path: Path, diff: _DiffSetter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the budget the cheap bar hands the head to the full evidence gate."""
    diff(["agent_fleet/foo.py"])
    answer = json.dumps(
        {
            "findings": [
                {
                    "id": "F1",
                    "file": "a.py",
                    "line": 3,
                    "claim": "off by one",
                    "repro": "x",
                    "testable": True,
                }
            ]
        }
    )
    backend = _FakeBackend(answer=answer)
    pipe = _pipeline(tmp_path, backend, _config())
    # This PR already spent its three passes on earlier heads.
    monkeypatch.setattr(
        "agent_fleet.gate.metrics.read_metrics",
        lambda *_a, **_k: [
            {
                "repo": "agent-fleet",
                "pr": 42,
                "tier": STANDARD_TIER,
                "outcome": OUTCOME_FIX_AND_REGATE,
            }
        ]
        * 3,
    )
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GatePipeline._standard_fixer",
        lambda *_a, **_k: pytest.fail("a spent budget must not dispatch another fixer"),
    )

    result = pipe.run_standard(_ref(), tmp_path / "wt", [])

    assert not result.approved
    assert result.metrics is not None
    assert result.metrics.outcome == OUTCOME_FALLBACK
    assert result.metrics.passes == 3
    assert "full evidence gate required" in result.reasons[0]
    assert "PREMERGE-APPROVED" not in result.status_line


def test_a_sensitive_diff_never_reaches_the_cheap_bar(
    tmp_path: Path, diff: _DiffSetter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The veto is enforced at the branch inside run(), not only in the pure function.

    Running the real entry point is what pins the wiring: select_tier() could be
    correct while run() forgets to call it, and the cheap bar would approve gold.
    """
    diff(["dags/gold/daily_sales.py", "agent_fleet/foo.py"])
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, _config())
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.fetch_base", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.prepare_worktree", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.remove_worktree", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.resolve_pull_request", lambda *_a, **_k: _ref()
    )
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GatePipeline.run_pr_tests", lambda _self, *_a, **_k: []
    )
    # The cheap bar must never be entered for a sensitive diff.
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.GatePipeline.run_standard",
        lambda *_a, **_k: pytest.fail("a sensitive diff reached the standard bar"),
    )

    result = pipe.run()

    # The full evidence pipeline ran. Its lens *count* is still the size-based
    # decision this small diff earns — sensitivity chooses the bar, not the
    # fan-out — so the load-bearing assertion is that the cheap bar was not
    # entered and that the row is not tagged as a STANDARD run.
    assert result.metrics is not None
    assert result.metrics.tier != STANDARD_TIER
    assert backend.prompts, "the sensitive diff must still be reviewed"
