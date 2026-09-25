"""``judge_backend=None`` must still mean "no judge" for direct GatePipeline callers.

``GatePipeline`` is a documented public export (its docstring: "for tests and
reuse"), and ``judge_backend`` is a keyword argument defaulting to ``None``. The
pre-per-role-backend contract was unambiguous: ``judge()`` and
``recheck_untestable()`` returned immediately when ``judge_backend is None``,
issuing no call at all. That is the only way a caller can turn the judge off
without also flipping ``enable_judge``.

The per-role-backend rewrite replaced the ``self.judge_backend is None`` guard
with ``self._role_backend(ROLE_JUDGE)``, which falls back to ``self.backend``.
A caller who passes ``judge_backend=None`` now gets a judge call they never
asked for, on the *find* backend: either a ``ModelPolicyError`` raised out of
``judge()`` mid-run when no judge model is pinned, or a silent judge pass that
can promote untestable claims to confirmed blockers.

These tests build the pipeline exactly as a public-API caller would and assert
the original "no judge" behaviour.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.gitops import PullRequestRef
from agent_fleet.gate.pipeline import GatePipeline
from agent_fleet.model_policy import ModelPolicy, ModelPolicyError, parse_model_policy

SPACE_BUNNY = "stealth/space-bunny-alpha"

_POLICY_SECTION = {
    "model_policy": {
        "backends": {
            "cmd": {"allowed_models": [SPACE_BUNNY]},
            "openrouter": {
                "allowed_models": [SPACE_BUNNY],
                "roles": ["find", "judge"],
            },
        }
    }
}

_JUDGE_ANSWER = json.dumps(
    {
        "untestable_rulings": [
            {"id": "c-1", "real": True, "reason": "the gate never tested it, so it is real"}
        ],
        "new_blockers": [],
    }
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """Replays a scripted answer and records every call it was given."""

    default: str = ""
    prompts: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    name: str = "fake"

    def run(self, prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        self.models.append(str(kwargs.get("model", "")))
        return _FakeResult(self.default)


@dataclass(frozen=True)
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny git repo on a branch off ``main`` (the gate diffs a PR branch)."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef div(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "branch change")
    return repo


def _pipeline(
    repo: Path,
    *,
    config: GateConfig,
    policy: ModelPolicy,
    backend: _FakeBackend,
    judge_backend: _FakeBackend | None,
) -> GatePipeline:
    """A pipeline built through the public constructor, by an external caller."""
    return GatePipeline(
        repo=repo,
        pr_number=7,
        config=config,
        policy=policy,
        backend=backend,
        judge_backend=judge_backend,
        gate_dir=repo.parent / "gate",
        use_systemd=False,
    )


def _ref() -> PullRequestRef:
    return PullRequestRef(number=7, head_ref="feat", head_sha="a" * 40, state="OPEN")


# ---------------------------------------------------------------------------
# judge(): judge_backend=None means "no judge"
# ---------------------------------------------------------------------------


def test_judge_with_none_judge_backend_makes_no_call(fixture_repo: Path) -> None:
    """The contract: judge_backend=None disables the judge without a call.

    ``judge_model`` is pinned so the policy check can never be what fails; the
    only way this can pass is the early return.
    """
    find = _FakeBackend(default=_JUDGE_ANSWER)
    config = GateConfig(
        backend="openrouter",
        model=SPACE_BUNNY,
        judge_backend="openrouter",
        judge_model=SPACE_BUNNY,
        enable_judge=True,
    )
    pipe = _pipeline(
        fixture_repo,
        config=config,
        policy=parse_model_policy(_POLICY_SECTION),
        backend=find,
        judge_backend=None,
    )
    assert pipe.judge_backend is None

    pipe.judge(fixture_repo, _ref())

    assert find.prompts == [], (
        "judge_backend=None means 'no judge': judge() must not dispatch to the "
        f"find backend, but it issued {len(find.prompts)} call(s)"
    )
    assert pipe.evidence.confirmed == [], (
        "with no judge backend, no untestable claim may be promoted to a "
        f"confirmed blocker; got {pipe.evidence.confirmed}"
    )


def test_judge_with_none_judge_backend_does_not_raise_policy_error(
    fixture_repo: Path,
) -> None:
    """With no judge model pinned, a disabled judge must not blow up mid-run.

    The dispatch resolves the model *before* calling anything, so falling
    through to the find backend raises out of ``judge()`` — a crash in the
    middle of a PR run over a step the caller disabled.
    """
    find = _FakeBackend(default=_JUDGE_ANSWER)
    config = GateConfig(
        backend="openrouter",
        model=SPACE_BUNNY,
        judge_backend="openrouter",
        judge_model=None,
        enable_judge=True,
    )
    pipe = _pipeline(
        fixture_repo,
        config=config,
        policy=parse_model_policy(_POLICY_SECTION),
        backend=find,
        judge_backend=None,
    )

    try:
        pipe.judge(fixture_repo, _ref())
    except ModelPolicyError as exc:  # pragma: no cover - the failure being pinned
        pytest.fail(
            "judge_backend=None must skip the judge entirely, but judge() "
            f"raised a policy error for the judge step: {exc}"
        )
    assert find.prompts == []


# ---------------------------------------------------------------------------
# recheck_untestable(): same contract
# ---------------------------------------------------------------------------


def test_recheck_with_none_judge_backend_makes_no_call(fixture_repo: Path) -> None:
    """``recheck_untestable`` kept the guard on main too; it must keep it."""
    find = _FakeBackend(default=json.dumps({"unresolved": []}))
    config = GateConfig(
        backend="openrouter",
        model=SPACE_BUNNY,
        judge_backend="openrouter",
        judge_model=SPACE_BUNNY,
        enable_judge=True,
    )
    pipe = _pipeline(
        fixture_repo,
        config=config,
        policy=parse_model_policy(_POLICY_SECTION),
        backend=find,
        judge_backend=None,
    )
    # The untestable blocker a recheck exists to re-examine.
    pipe.evidence.confirmed.append(
        {"id": "c-1", "source": "judge-untestable", "claim": "unproven", "test_file": None}
    )

    try:
        resolved = pipe.recheck_untestable(fixture_repo, "b" * 40, "a" * 40)
    except ModelPolicyError as exc:  # pragma: no cover - the failure being pinned
        pytest.fail(
            "judge_backend=None must skip the recheck entirely, but it raised a "
            f"policy error for the judge step: {exc}"
        )

    assert find.prompts == [], (
        "recheck_untestable must not dispatch when judge_backend=None, but it "
        f"issued {len(find.prompts)} call(s)"
    )
    assert resolved is True
