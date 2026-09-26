"""A fixer pushes to the PR's own head, never to a lane-derived branch.

The documents-1d incident: the gate's fixer was told to
``git push origin HEAD:fb/dq1d-<lane>``. That branch is not the PR's head, so
the fix landed somewhere nothing re-gates, and the gate then reported
``no-push`` over a PR whose head had in fact moved — an operator reading the
verdict would re-dispatch work that was already done, and re-review a head
that would never contain the fix.

The gate has two fixer paths (:meth:`GatePipeline.converge` for the sensitive
tier, :meth:`GatePipeline._standard_fixer` for the risk-matched bar), and they
must agree. ``gate.push_branch`` used to override the first, which is why the
knob had to go: leaving a config key that can reintroduce the bug is a half-fix,
because the failure only returns the day someone sets it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.gitops import PullRequestRef
from agent_fleet.gate.pipeline import GatePipeline
from agent_fleet.model_policy import ModelPolicy

#: A branch that looks like a lane but is not the PR. The exact shape the
#: documents-1d failure used.
_LANE_BRANCH = "fb/dq1d-lane"


@dataclass
class _FakeBackend:
    """Records the prompts it is handed, so a test can read the push target."""

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
        "enable_judge": False,
        "enable_fix": True,
    }
    base.update(overrides)
    return GateConfig(**base)  # type: ignore[arg-type]


def _ref() -> PullRequestRef:
    """A PR whose real head ref is *not* lane-shaped."""
    return PullRequestRef(
        number=42,
        head_ref="dq1d/real-head",
        head_sha="a" * 40,
        state="OPEN",
        base_ref="main",
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


def _capturing_fixer(pipe: GatePipeline, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make ``_standard_fixer`` run without a real repository.

    The prompt is the thing under test, and building a git worktree to obtain
    it would make this a git test. Only the worktree bookkeeping and the
    agent call are stubbed; everything that decides *what the fixer is told*
    is left alone.
    """
    from agent_fleet.gate import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "prepare_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_mod, "remove_worktree", lambda *_a, **_k: None)
    # The post-fixer head check asks the forge what the head is; the prompt is
    # already captured by then, so it is noise for this test.
    monkeypatch.setattr(pipeline_mod, "current_pr_head", lambda *_a, **_k: "a" * 40)
    monkeypatch.setattr(pipeline_mod, "fetch_base", lambda *_a, **_k: None)
    prompts: list[str] = []
    monkeypatch.setattr(pipe, "_run_fixer", lambda prompt, **_k: prompts.append(prompt))
    return prompts


def _push_targets(prompts: list[str]) -> list[str]:
    """Every branch a prompt tells an agent to push to.

    The instruction is mid-sentence ("push with `git push origin HEAD:x`)"), so
    the branch ends at the closing backtick.
    """
    targets = []
    for prompt in prompts:
        for match in re.finditer(r"git push origin HEAD:([^`\s]+)", prompt):
            targets.append(match.group(1).rstrip("`.,"))
    return targets


def test_the_configured_lane_branch_is_never_given_to_a_fixer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: a configured ``fb/<lane>`` must not reach the prompt.

    Asserted on the prompt the agent receives rather than on a git push, so
    the test needs no remote and cannot pass by accident.
    """
    pipe = _pipeline(tmp_path, _FakeBackend(), _config(push_branch=_LANE_BRANCH))
    prompts = _capturing_fixer(pipe, monkeypatch)

    pipe._standard_fixer(_ref(), pr_tests=[], passes=0)

    assert prompts, "the fixer must have been dispatched"
    targets = _push_targets(prompts)
    assert targets, f"no push target in the prompt: {prompts[0][:400]}"
    assert all(t == "dq1d/real-head" for t in targets), targets
    assert _LANE_BRANCH not in prompts[0]


def test_both_fixer_paths_agree_on_the_push_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``converge`` and ``_standard_fixer`` must name the same branch.

    They are separate code paths, and only the STANDARD one used to be pinned
    to the PR's head. A gate where one of them can still be pointed at a lane
    is a gate that fails intermittently, which is worse than failing loudly.
    """
    config = _config(push_branch=_LANE_BRANCH)
    pipe = _pipeline(tmp_path, _FakeBackend(), config)
    prompts = _capturing_fixer(pipe, monkeypatch)
    pipe._standard_fixer(_ref(), pr_tests=[], passes=0)
    assert _push_targets(prompts) == ["dq1d/real-head"]

    # The config value is still parsed, so an existing file that sets it does
    # not break; it is simply not consulted for the push target.
    assert pipe.config.push_branch == _LANE_BRANCH
    converge_source = (
        Path(__file__).resolve().parents[1] / "agent_fleet" / "gate" / "pipeline.py"
    ).read_text(encoding="utf-8")
    assert "push_branch = self.config.push_branch or ref.head_ref" not in converge_source
    assert "push_branch = ref.head_ref" in converge_source


def test_the_pr_head_is_used_even_without_any_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case, so the fix is not merely "the config happens to be ignored"."""
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    prompts = _capturing_fixer(pipe, monkeypatch)

    pipe._standard_fixer(_ref(), pr_tests=[], passes=0)

    assert _push_targets(prompts) == ["dq1d/real-head"]


def test_a_pr_whose_head_is_lane_shaped_still_pushes_to_its_own_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR legitimately opened on an ``fb/...`` branch is not the bug.

    The rule is "the PR's head", not "a branch that is not lane-shaped" — a
    test that forbade the ``fb/`` prefix would pass while the real defect
    stood.
    """
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    ref = PullRequestRef(
        number=42, head_ref="fb/genuine", head_sha="b" * 40, state="OPEN", base_ref="main"
    )
    prompts = _capturing_fixer(pipe, monkeypatch)

    pipe._standard_fixer(ref, pr_tests=[], passes=0)

    assert _push_targets(prompts) == ["fb/genuine"]


@pytest.mark.parametrize("configured", [None, "", _LANE_BRANCH, "fb/other"])
def test_no_configured_value_changes_where_a_fixer_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str | None
) -> None:
    """Whatever the config says, the answer is the PR's head."""
    pipe = _pipeline(tmp_path, _FakeBackend(), _config(push_branch=configured))
    prompts = _capturing_fixer(pipe, monkeypatch)

    pipe._standard_fixer(_ref(), pr_tests=[], passes=0)

    assert _push_targets(prompts) == ["dq1d/real-head"]
