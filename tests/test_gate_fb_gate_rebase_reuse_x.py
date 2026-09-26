"""Reuse must never spend the evidence this run already established.

all-1: the reuse branch in :meth:`GatePipeline.run` replaced the whole evidence
object with the marker's payload. But step 0 runs *before* that branch, and a
PR test failing at head is already a confirmed blocker on this head — so the
replacement threw it away. The marker in question holds **no** confirmed
blockers (the earlier run found none, which is why it approved), so after the
swap ``self.evidence.confirmed`` was empty and the gate took the "nothing
confirmed, therefore approved" branch: a red PR merged behind a
``PREMERGE-APPROVED`` line.

The test drives the real ``run()`` with a finished verify marker at this head,
so it fails on the whole path rather than on a helper: the guard has to hold at
the point where reuse is applied, not merely in the code that stores it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.contracts.gate import GateOutcome
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.gitops import MergeConflict, PullRequestRef
from agent_fleet.gate.pipeline import GatePipeline, GateTestRunner, TestRun
from agent_fleet.model_policy import ModelPolicy

_HEAD = "a" * 40
_PR_TEST = "tests/test_prod.py"
_NODE = f"{_PR_TEST}::test_x"


@dataclass
class _FakeBackend:
    """A backend that finds nothing — so reuse, not a review, decides the verdict."""

    prompts: list[str] = field(default_factory=list)

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        return type(
            "R", (), {"exit_code": 0, "stdout": json.dumps({"findings": []}), "stderr": ""}
        )()


def _ref() -> PullRequestRef:
    return PullRequestRef(
        number=42, head_ref="fb/lane", head_sha=_HEAD, state="OPEN", base_ref="main"
    )


def _config(**overrides: object) -> GateConfig:
    base: dict[str, object] = {
        "backend": "cmd",
        "model": "m",
        "judge_backend": "cmd",
        "enable_judge": False,
        "enable_fix": False,
    }
    base.update(overrides)
    return GateConfig(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's real metrics rows out of a verdict."""
    monkeypatch.setattr("agent_fleet.gate.metrics.read_metrics", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "agent_fleet.gate.metrics.GateMetrics.append_metrics",
        lambda _self, *_a, **_k: None,
    )


@pytest.fixture
def red_pr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[GatePipeline, _FakeBackend]:
    """A PR whose own changed test fails at head, and a finished prior run.

    The diff touches a *sensitive* path, so tier selection sends it down the
    full evidence pipeline where the reuse branch lives — the defect does not
    reach STANDARD, which returns before it.
    """
    pipe = GatePipeline(
        repo=tmp_path / "repo",
        pr_number=42,
        config=_config(),
        policy=ModelPolicy(backends={}),
        backend=_FakeBackend(),  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )
    backend = _FakeBackend()
    pipe.backend = backend  # type: ignore[assignment]

    for name in ("fetch_base", "prepare_worktree", "remove_worktree"):
        monkeypatch.setattr(f"agent_fleet.gate.pipeline.{name}", lambda *_a, **_k: None)
    monkeypatch.setattr("agent_fleet.gate.pipeline.resolve_pull_request", lambda *_a, **_k: _ref())
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.merge_conflict_check",
        lambda *_a, **_k: MergeConflict(conflict_files=(), git_error=False),
    )
    # The PR changes ordinary product code plus a test of it, and touches a path
    # the config calls sensitive: tier 0 and the STANDARD bar both refuse, and
    # the full pipeline — the only path with the reuse branch — is what runs.
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.changed_paths",
        lambda *_a, **_k: ["agent_fleet/sales_schema.py", _PR_TEST],
    )
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.changed_test_files", lambda *_a, **_k: [_PR_TEST]
    )
    monkeypatch.setattr("agent_fleet.gate.pipeline.diff_line_stats", lambda *_a, **_k: 10)
    monkeypatch.setattr("agent_fleet.gate.pipeline.prodsensitive_paths", lambda *_a, **_k: [])
    monkeypatch.setattr("agent_fleet.gate.pipeline.deleted_test_paths", lambda *_a, **_k: [])
    monkeypatch.setattr("agent_fleet.gate.pipeline.changed_test_config_paths", lambda *_a, **_k: [])
    monkeypatch.setattr(
        GateTestRunner,
        "run",
        lambda _self, _files: TestRun(failing=[_NODE], ran=1, tests_failed=True),
    )
    return pipe, backend


def test_reuse_keeps_the_pr_test_blocker_this_head_already_proved(
    red_pr: tuple[GatePipeline, _FakeBackend],
) -> None:
    """The regression: a reused marker must not erase step 0's own blocker.

    A prior run at this head finished verification and found nothing, so its
    marker records an empty ``confirmed``. Standing on that marker is the whole
    point of reuse — but the PR's own test is red *now*, and that verdict is
    made here, not inherited.
    """
    pipe, _backend = red_pr
    pipe.state.mark_verified(
        _HEAD,
        {"evidence": {"confirmed": [], "untestable": [], "gate_tests": []}, "candidates": []},
        patch_id="",
        outcome=GateOutcome.APPROVED.value,
    )

    result = pipe.run()

    assert result.outcome is not GateOutcome.APPROVED
    assert "PREMERGE-APPROVED" not in result.status_line
    assert any("PR test fails at head" in reason for reason in result.reasons), result.reasons
    assert [c["id"] for c in result.confirmed] == ["T-test_x"]


def test_reuse_also_keeps_a_reused_blocker_next_to_this_heads_own(
    red_pr: tuple[GatePipeline, _FakeBackend],
) -> None:
    """Merging must add the marker's blockers, not only protect step 0's.

    A wholesale replacement also lost every *lens* finding the prior run had
    confirmed, which is the same silent-downgrade in the other direction: the
    head is green, so the earlier run escalated, and reuse would approve it.
    """
    pipe, _backend = red_pr
    pipe.state.mark_verified(
        _HEAD,
        {
            "evidence": {
                "confirmed": [
                    {
                        "id": "L-1",
                        "source": "all-focus",
                        "claim": "prior blocker at this head",
                        "test_file": "tests/test_gate_fb_lane_x.py",
                    }
                ],
                "untestable": [],
                "gate_tests": ["tests/test_gate_fb_lane_x.py"],
            },
            "candidates": [],
        },
        patch_id="",
        outcome=GateOutcome.NEEDS_ESCALATION.value,
    )

    result = pipe.run()

    ids = [c["id"] for c in result.confirmed]
    assert "L-1" in ids, ids
    assert "T-test_x" in ids, ids


def test_merging_the_same_marker_twice_does_not_double_count(
    red_pr: tuple[GatePipeline, _FakeBackend],
) -> None:
    """Reuse is keyed by head, so the same blocker can arrive from two markers.

    A same-head marker and a patch-id marker for one head are two records of one
    finding. Counting it twice would make a single defect look like two and
    inflate the escalation's reason list with the same claim.
    """
    pipe, _backend = red_pr
    payload = {
        "evidence": {
            "confirmed": [
                {
                    "id": "L-1",
                    "source": "all-focus",
                    "claim": "prior blocker at this head",
                    "test_file": "tests/test_gate_fb_lane_x.py",
                }
            ],
            "untestable": [],
            "gate_tests": ["tests/test_gate_fb_lane_x.py"],
        }
    }
    pipe.state.mark_verified(
        _HEAD, payload, patch_id="", outcome=GateOutcome.NEEDS_ESCALATION.value
    )

    result = pipe.run()

    assert [c["id"] for c in result.confirmed].count("L-1") == 1


def test_a_green_pr_still_approves_on_a_reused_marker(
    red_pr: tuple[GatePipeline, _FakeBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge must not turn reuse into "nothing is ever approved".

    Same head, same marker, same reuse — the only change is that the PR's own
    test passes. With no blocker to keep, the run is back to what reuse is for:
    no reviewer dispatched, and the head approved on the reused evidence.
    """
    pipe, backend = red_pr
    monkeypatch.setattr(
        GateTestRunner, "run", lambda _self, _files: TestRun(ran=1, tests_failed=False)
    )
    pipe.state.mark_verified(
        _HEAD,
        {"evidence": {"confirmed": [], "untestable": [], "gate_tests": []}, "candidates": []},
        patch_id="",
        outcome=GateOutcome.APPROVED.value,
    )

    result = pipe.run()

    assert result.outcome is GateOutcome.APPROVED, result.reasons
    assert not backend.prompts, "reuse exists so the review is not re-bought"
