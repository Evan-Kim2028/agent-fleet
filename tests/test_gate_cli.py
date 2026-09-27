"""Tests for the `agent-fleet gate` CLI wiring."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.cli import main
from agent_fleet.contracts.gate import GateOutcome
from agent_fleet.gate.pipeline import GateResult


class _StubResult(GateResult):
    """A GateResult that does no work, for CLI-level assertions."""


def _result(outcome: GateOutcome, sha: str = "abc123def456") -> GateResult:
    result = GateResult(outcome=outcome, sha=sha, run_id="gate-test")
    result.status_line = (
        f"10:00:00 PREMERGE-APPROVED {sha[:9]}"
        if outcome is GateOutcome.APPROVED
        else "10:00:00 NEEDS-ESCALATION stalled after 1 round(s)"
    )
    return result


def _patch_gate(monkeypatch: pytest.MonkeyPatch, result: GateResult) -> list[Path]:
    calls: list[Path] = []

    def _fake_run_gate(**kwargs: Any) -> GateResult:  # noqa: ANN401
        calls.append(kwargs["repo_path"])
        return result

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _fake_run_gate)
    return calls


def test_gate_requires_a_pr_number(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gate"]) == 2
    assert "--pr" in capsys.readouterr().err


def test_gate_approved_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_gate(monkeypatch, _result(GateOutcome.APPROVED))
    assert main(["gate", "--repo-path", str(tmp_path), "--pr", "42"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "APPROVED"
    assert payload["sha"] == "abc123def456"


def test_gate_escalation_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 0 only on APPROVED, so an automerge wrapper can gate on the code."""
    _patch_gate(monkeypatch, _result(GateOutcome.NEEDS_ESCALATION, ""))
    assert main(["gate", "--repo-path", str(tmp_path), "--pr", "42"]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "NEEDS_ESCALATION"


def test_gate_prints_the_status_line_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The status line is the automerge contract; keep it off stdout (JSON)."""
    _patch_gate(monkeypatch, _result(GateOutcome.APPROVED))
    main(["gate", "--repo-path", str(tmp_path), "--pr", "1"])
    captured = capsys.readouterr()
    assert "PREMERGE-APPROVED abc123def" in captured.err
    json.loads(captured.out)  # stdout stays valid JSON


def test_gate_passes_through_task_and_status_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def _fake_run_gate(**kwargs: Any) -> GateResult:  # noqa: ANN401
        seen.update(kwargs)
        return _result(GateOutcome.APPROVED)

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _fake_run_gate)
    main(
        [
            "gate",
            "--repo-path",
            str(tmp_path),
            "--pr",
            "9",
            "--task-file",
            "/tmp/task.md",
            "--status-file",
            "/tmp/status",
        ]
    )
    assert seen["pr_number"] == 9
    assert str(seen["task_file"]) == "/tmp/task.md"
    assert str(seen["status_file"]) == "/tmp/status"
    assert seen["repo_path"] == tmp_path


def test_gate_surfaces_a_policy_violation_as_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model the policy forbids must fail before any agent is dispatched."""
    from agent_fleet.model_policy import ModelPolicyError

    def _boom(**_kwargs: Any) -> GateResult:  # noqa: ANN401
        raise ModelPolicyError("model_policy: model 'x' not allowed for backend 'cmd'")

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _boom)
    assert main(["gate", "--repo-path", str(tmp_path), "--pr", "1"]) == 1
    assert "model_policy" in capsys.readouterr().err


def test_gate_passes_the_lane_slug_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The slug makes gate test names unique per PR; the CLI must reach run_gate."""
    seen: dict[str, Any] = {}

    def _fake_run_gate(**kwargs: Any) -> GateResult:  # noqa: ANN401
        seen.update(kwargs)
        return _result(GateOutcome.APPROVED)

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _fake_run_gate)
    main(["gate", "--repo-path", str(tmp_path), "--pr", "1", "--lane-slug", "fb/lane"])
    assert seen["lane_slug"] == "fb/lane"


def test_gate_lane_slug_defaults_to_none_so_the_head_ref_supplies_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def _fake_run_gate(**kwargs: Any) -> GateResult:  # noqa: ANN401
        seen.update(kwargs)
        return _result(GateOutcome.APPROVED)

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _fake_run_gate)
    main(["gate", "--repo-path", str(tmp_path), "--pr", "1"])
    assert seen["lane_slug"] is None


def test_gate_metrics_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.gate_metrics_summary",
        lambda **_kw: {"recent": [{"pr": 1}], "table": "T", "summary": {"runs": 1}},
    )
    assert main(["gate", "metrics"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"] == {"runs": 1}


def test_gate_metrics_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.gate_metrics_summary",
        lambda **_kw: {"recent": [], "table": "the table", "summary": {}},
    )
    assert main(["gate", "metrics", "--format", "table"]) == 0
    assert capsys.readouterr().out.strip() == "the table"


def test_gate_metrics_limit_is_passed_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, int] = {}
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.gate_metrics_summary",
        lambda limit=20: (seen.setdefault("limit", limit) and {}) or {"recent": []},
    )
    main(["gate", "metrics", "--limit", "3"])
    capsys.readouterr()
    assert seen["limit"] == 3


def test_gate_is_registered_as_a_subcommand() -> None:
    """A typo must not be normalised into a task goal and dispatched."""
    with pytest.raises(SystemExit) as exc:
        main(["gat", "--pr", "1"])
    assert exc.value.code != 0


def test_the_judge_engine_flag_reaches_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--judge-engine`` is a documented override, so it has to arrive.

    The dispatcher emits this flag whenever a judge engine is configured, so
    dropping it at the consumer made the flag and the ``judge_engine`` config key
    inert for every gate the queue spawns.
    """
    seen: dict[str, Any] = {}

    def _fake_run_gate(**kwargs: Any) -> GateResult:  # noqa: ANN401
        seen.update(kwargs)
        return _result(GateOutcome.APPROVED)

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _fake_run_gate)
    main(["gate", "--repo-path", str(tmp_path), "--pr", "1", "--judge-engine", "grok"])
    assert seen["judge_engine"] == "grok"


def test_no_judge_engine_flag_means_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent means absent, so the configured judge backend is what runs."""
    seen: dict[str, Any] = {}

    def _fake_run_gate(**kwargs: Any) -> GateResult:  # noqa: ANN401
        seen.update(kwargs)
        return _result(GateOutcome.APPROVED)

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate", _fake_run_gate)
    main(["gate", "--repo-path", str(tmp_path), "--pr", "1"])
    assert seen["judge_engine"] is None


def test_a_gate_pointed_at_the_wrong_repo_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--repo`` is a cross-check, so naming another team's repo must fail.

    A gate that silently reviewed a different repository is the failure this
    flag exists to prevent, so the run is refused rather than warned about.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("agent_fleet.gate.gitops.origin_slug", lambda _p: "Evan-Kim2028/real-repo")
    called = _patch_gate(monkeypatch, _result(GateOutcome.APPROVED))
    assert (
        main(["gate", "--repo-path", str(repo), "--pr", "1", "--repo", "someone-else/other"]) == 1
    )
    assert called == [], "the gate must not run when its target is wrong"


def test_a_gate_naming_its_own_repo_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-check must not refuse a correct gate, or it disables the gate."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("agent_fleet.gate.gitops.origin_slug", lambda _p: "Evan-Kim2028/acme")
    _patch_gate(monkeypatch, _result(GateOutcome.APPROVED))
    assert main(["gate", "--repo-path", str(repo), "--pr", "1", "--repo", "Evan-Kim2028/acme"]) == 0


def test_a_gate_pointed_at_a_stale_head_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--head-ref`` must match the PR's real head, not the caller's belief."""
    from agent_fleet.gate.gitops import PullRequestRef

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "agent_fleet.gate.gitops.resolve_pull_request",
        lambda _r, n: PullRequestRef(
            number=n, head_ref="fb/actual", head_sha="a" * 40, state="OPEN"
        ),
    )
    called = _patch_gate(monkeypatch, _result(GateOutcome.APPROVED))
    assert main(["gate", "--repo-path", str(repo), "--pr", "1", "--head-ref", "fb/stale"]) == 1
    assert called == []
