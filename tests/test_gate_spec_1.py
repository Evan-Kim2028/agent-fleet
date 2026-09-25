"""Claim spec-1: the judge recheck must inline the change for a no-tool backend.

``gate.roles.judge.backend = openrouter`` puts the *judge* on a backend with no
repo tools, so ``find`` and ``judge`` both paste the change into the prompt and
drop the ``git`` directives. ``recheck_untestable`` dispatches to that same
openrouter judge backend but builds its prompt without the inlined context, so
it asks a shell-less model to run ``git fetch``/``git diff`` and never shows it
the change at all — yet its "nothing unresolved" answer alone clears a confirmed
blocker and can flip the run to APPROVED.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.pipeline import GatePipeline
from agent_fleet.model_policy import parse_model_policy

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


@dataclass
class _FakeBackend:
    """Replays a scripted answer and records every prompt it was handed."""

    answers: dict[str, str] = field(default_factory=dict)
    default: str = ""
    prompts: list[str] = field(default_factory=list)
    name: str = "fake"

    def run(self, prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        for needle, answer in self.answers.items():
            if needle in prompt:
                return _FakeResult(answer)
        return _FakeResult(self.default)


@dataclass(frozen=True)
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A repo whose feature branch carries a real regression in changed code."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "def test_add():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\n"
        "def div(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "branch change")
    return repo


def _roles_config() -> GateConfig:
    cfg = load_gate_config(
        {
            "gate": {
                "backend": "cmd",
                "model": SPACE_BUNNY,
                "judge_backend": "cmd",
                "judge_model": SPACE_BUNNY,
                "enable_judge": True,
                "enable_fix": True,
                "roles": {
                    "find": {"backend": "openrouter", "model": SPACE_BUNNY},
                    "judge": {"backend": "openrouter", "model": SPACE_BUNNY},
                },
                "lenses": ["correctness"],
                "agent_timeout_s": 5,
                "judge_timeout_s": 5,
            }
        }
    )
    assert cfg is not None
    return cfg


def _pipeline(tmp_path: Path, repo: Path, cmd: _FakeBackend, remote: _FakeBackend) -> GatePipeline:
    config = _roles_config()
    pipe = GatePipeline(
        repo=repo,
        pr_number=7,
        config=config,
        policy=parse_model_policy(_POLICY_SECTION),
        backend=cmd,
        judge_backend=remote,
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )
    by_name = {"cmd": cmd, "openrouter": remote}
    pipe._role_backends = {r: by_name[config.role_target(r).backend] for r in ("find", "judge", "verify", "fix")}
    return pipe


def test_recheck_inlines_the_change_for_a_no_tool_judge(
    tmp_path: Path, fixture_repo: Path
) -> None:
    cmd = _FakeBackend(name="cmd")
    remote = _FakeBackend(
        default=json.dumps({"unresolved": []}),
        name="openrouter",
    )
    pipe = _pipeline(tmp_path, fixture_repo, cmd, remote)

    # A blocker the judge earlier ruled real and untestable, still open.
    pipe.evidence.confirmed.append(
        {
            "id": "u-1",
            "source": "judge-untestable",
            "claim": "div() divides by zero, no unit test can show it",
            "test_file": None,
        }
    )

    start = _git(fixture_repo, "rev-parse", "main").strip()
    head = _git(fixture_repo, "rev-parse", "HEAD").strip()

    assert pipe.recheck_untestable(fixture_repo, start, head) is True
    assert len(remote.prompts) == 1, "the judge recheck must reach the openrouter judge backend"
    prompt = remote.prompts[0]

    # The judge runs on OpenRouter: it has no shell and no repo access, so the
    # change must be pasted in and the git commands withdrawn — the same
    # treatment find() and judge() already get.
    assert "INLINED CHANGE" in prompt, (
        "recheck_prompt must inline the change for a backend with no repo tools"
    )
    assert "def div(a, b):" in prompt, "the recheck prompt must carry the change being rechecked"
    assert "NO tools, NO shell" in prompt
    # No shell: the prompt must not order git commands the model cannot run.
    assert "use a fresh `git fetch`" not in prompt
    assert "inspect `git diff" not in prompt
