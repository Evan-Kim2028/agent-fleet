"""The reviewer brief: the change inline, the PR's own base, and turn caps.

A gate fan-out pays twice for every reviewer turn — once to run it, once to
re-upload the context it grew — and a reviewer pointed at ``git diff`` goes and
gets it, ~44 calls per reviewer. So the change is diffed once and embedded in the
prompt, the reviewer is told the budget and the one legitimate reason to open a
file, and the diff is the PR's *own* base rather than main.

These tests pin the three properties that make that work, each of which fails
silently when it breaks:

1. the prompt carries the change, not just a command to fetch it;
2. a truncated diff *says* so, because a silently clipped diff reads as the
   complete change and the dropped half is never reviewed;
3. every diff the gate takes — reviewers, verifiers, judge, merged-tree
   regression check — resolves against the base the PR actually targets.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.gate import gitops
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.gitops import (
    PullRequestRef,
    inline_change,
    resolve_base_branch,
)
from agent_fleet.gate.pipeline import GatePipeline, GateTestRunner, TestRun
from agent_fleet.gate.prompts import find_prompt
from agent_fleet.model_policy import ModelPolicy

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "gate-fixture"
version = "0.0.0"
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a main branch and a stacked feature branch on top of it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    # A long pre-existing file, so a test can edit its middle and read the
    # context lines around the edit (a brand-new file has none).
    (root / "big.py").write_text("".join(f"L{i} = {i}\n" for i in range(120)), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _git(root, "checkout", "-q", "-b", "stack-parent")
    (root / "parent_only.py").write_text("PARENT = 1\n", encoding="utf-8")
    _commit(root, "parent work")

    _git(root, "checkout", "-q", "-b", "stack-child")
    (root / "agent.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_agent.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")
    _commit(root, "child work")
    return root


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class _FakeBackend:
    """Records every prompt and the turn cap it was dispatched with."""

    answer: str = '```json\n{"findings": []}\n```'
    prompts: list[str] = field(default_factory=list)
    turns: list[Any] = field(default_factory=list)

    def run(self, prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        self.turns.append(kwargs.get("max_turns"))
        return _FakeResult(self.answer)


def _pipeline(tmp_path: Path, backend: _FakeBackend, **overrides: Any) -> GatePipeline:  # noqa: ANN401
    base: dict[str, Any] = {
        "backend": "cmd",
        "model": "m",
        "judge_backend": "cmd",
        "judge_model": "m",
        "enable_judge": False,
        "enable_fix": False,
        "lenses": ("correctness",),
    }
    base.update(overrides)
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=42,
        config=GateConfig(**base),
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


# ---------------------------------------------------------------------------
# The prompt carries the change
# ---------------------------------------------------------------------------


def test_find_prompt_embeds_the_change_instead_of_only_naming_it() -> None:
    prompt = find_prompt(
        lens="correctness",
        focus="logic errors",
        worktree="/tmp/wt",
        base_branch="origin/stack-parent",
        head_sha="abc123def",
        pr_number=7,
        task_text="the task",
        change="diff --git a/agent.py b/agent.py\n-VALUE = 1\n+VALUE = 2",
        change_note="(complete)",
    )
    assert "```diff" in prompt
    assert "+VALUE = 2" in prompt
    # The instruction not to re-derive it is what saves the calls; without it a
    # reviewer that sees the diff may still run git diff to confirm it.
    assert "do not re-run git diff" in prompt


def test_find_prompt_states_the_tool_budget_and_when_to_open_a_file() -> None:
    prompt = find_prompt(
        lens="correctness",
        focus="logic errors",
        worktree="/tmp/wt",
        base_branch="origin/main",
        head_sha="abc123def",
        pr_number=7,
        task_text="the task",
        change="diff",
        change_note="(complete)",
    )
    assert "~30 tool calls" in prompt
    assert "confirm or reject a specific suspected blocker" in prompt
    # A budget with no rule about what to skip is just a deadline.
    assert "never to browse" in prompt


def test_find_prompt_without_a_change_falls_back_to_naming_the_command() -> None:
    """A repo with no diffable change must not ship an empty ```diff block."""
    prompt = find_prompt(
        lens="correctness",
        focus="logic errors",
        worktree="/tmp/wt",
        base_branch="origin/main",
        head_sha="abc123def",
        pr_number=7,
        task_text="the task",
    )
    assert "```diff" not in prompt
    assert "git diff origin/main...HEAD" in prompt


def test_find_prompt_carries_the_truncation_note_verbatim() -> None:
    prompt = find_prompt(
        lens="correctness",
        focus="logic errors",
        worktree="/tmp/wt",
        base_branch="origin/main",
        head_sha="abc123def",
        pr_number=7,
        task_text="the task",
        change="diff --git a/x b/x",
        change_note="(TRUNCATED at 150000 chars: run git diff for the rest)",
    )
    assert "TRUNCATED at 150000 chars" in prompt


# ---------------------------------------------------------------------------
# inline_change: what the reviewers are handed
# ---------------------------------------------------------------------------


def test_inline_change_excludes_tests_and_markdown(repo: Path) -> None:
    change = inline_change(repo, "stack-parent", max_chars=150000)
    assert "+VALUE = 2" in change.text
    # The PR's own test and its markdown are the deterministic half's business
    # and prose respectively; neither can be a blocker.
    assert "test_agent.py" not in change.text
    assert "notes.md" not in change.text
    # The parent branch's own commit is not this PR's change.
    assert "parent_only.py" not in change.text
    assert not change.truncated


def test_inline_change_excludes_nested_tests_and_markdown(repo: Path) -> None:
    """The exclusions must reach paths with a directory in them.

    A bare ``:(exclude)*.md`` and ``:(exclude)tests/`` do not match
    ``docs/CHANGELOG.md`` or ``api/tests/x.py`` at all, so a repo that keeps its
    tests in a sub-package would hand every reviewer the whole test diff — the
    very thing the exclusion exists to avoid.
    """
    for rel, body in {
        "api/tests/test_nested.py": "def t():\n    pass\n",
        "api/tests/helper.py": "H = 1\n",
        "docs/CHANGELOG.md": "# changelog\n",
        "api/src/app.py": "A = 1\n",
    }.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _commit(repo, "nested dirs")
    change = inline_change(repo, "stack-parent", max_chars=150000)
    assert "api/src/app.py" in change.text
    assert "test_nested.py" not in change.text
    assert "api/tests/helper.py" not in change.text
    assert "CHANGELOG.md" not in change.text


def test_inline_change_diffs_against_the_named_base_not_main(repo: Path) -> None:
    """A stacked PR must not be reviewed against commits it never made."""
    against_parent = inline_change(repo, "stack-parent", max_chars=150000)
    against_main = inline_change(repo, "main", max_chars=150000)
    assert "parent_only.py" in against_main.text
    assert "parent_only.py" not in against_parent.text


def test_inline_change_truncates_and_says_so(repo: Path) -> None:
    change = inline_change(repo, "stack-parent", max_chars=40)
    assert len(change.text) == 40
    assert change.truncated
    assert "TRUNCATED at 40 chars" in change.note


def test_inline_change_within_the_cap_is_complete(repo: Path) -> None:
    change = inline_change(repo, "stack-parent", max_chars=150000)
    assert not change.truncated
    assert change.note == "(complete)"


def test_inline_change_wide_context_shows_the_surrounding_lines(repo: Path) -> None:
    """25 lines of context: enough to see the guard a change is about to break."""
    # big.py is 120 lines in the base commit; edit its middle, 60 lines in.
    (repo / "big.py").write_text(
        "".join(f"L{i} = {i}\n" for i in range(60))
        + "TAIL = 1\n"
        + "".join(f"L{i} = {i}\n" for i in range(60, 120)),
        encoding="utf-8",
    )
    _commit(repo, "edit big")
    change = inline_change(repo, "main", max_chars=150000)
    assert gitops.DIFF_CONTEXT_LINES == 25
    # The window is 25 lines either side of the insertion: L35..L59 above it and
    # L60..L84 below. L34 and L85 fall outside and must not be here.
    assert "-L34 = 34" not in change.text
    assert " L35 = 35" in change.text
    assert " L59 = 59" in change.text
    assert " L84 = 84" in change.text
    assert " L85 = 85" not in change.text


def test_inline_change_on_an_unchanged_repo_is_empty_not_truncated(repo: Path) -> None:
    _git(repo, "checkout", "-q", "stack-parent")
    change = inline_change(repo, "stack-parent", max_chars=150000)
    assert change.text == ""
    assert not change.truncated


# ---------------------------------------------------------------------------
# resolve_base_branch: the PR's own base
# ---------------------------------------------------------------------------


def test_base_branch_prefers_the_prs_own_base() -> None:
    ref = PullRequestRef(
        number=7, head_ref="fb/child", head_sha="a" * 40, state="OPEN", base_ref="fb/parent"
    )
    assert resolve_base_branch(ref, "main") == "fb/parent"


def test_base_branch_falls_back_to_config_when_the_pr_reports_none() -> None:
    ref = PullRequestRef(number=7, head_ref="fb/child", head_sha="a" * 40, state="OPEN")
    assert resolve_base_branch(ref, "main") == "main"
    assert resolve_base_branch(None, "main") == "main"


def test_base_branch_passes_an_explicit_ref_through() -> None:
    """An explicit origin/<branch> or sha is the operator; never overridden."""
    ref = PullRequestRef(
        number=7, head_ref="fb/child", head_sha="a" * 40, state="OPEN", base_ref="fb/parent"
    )
    assert resolve_base_branch(ref, "origin/main") == "origin/main"
    assert resolve_base_branch(ref, "refs/heads/main") == "refs/heads/main"
    assert resolve_base_branch(ref, "abc1234") == "abc1234"


# ---------------------------------------------------------------------------
# The pipeline: the base is resolved once and used everywhere
# ---------------------------------------------------------------------------


def test_pipeline_defaults_to_the_configured_base_until_the_pr_is_known(
    tmp_path: Path,
) -> None:
    backend = _FakeBackend()
    pipeline = _pipeline(tmp_path, backend, base_branch="trunk")
    assert pipeline.base_ref == "trunk"


def test_find_hands_the_reviewers_the_prs_own_base_and_the_change(
    tmp_path: Path, repo: Path
) -> None:
    backend = _FakeBackend()
    pipeline = _pipeline(tmp_path, backend, base_branch="main")
    pipeline.base_ref = "stack-parent"
    ref = PullRequestRef(
        number=42,
        head_ref="fb/child",
        head_sha="abcdef123456",
        state="OPEN",
        base_ref="stack-parent",
    )
    pipeline.find(repo, ref)  # type: ignore[arg-type]
    assert backend.prompts, "the lens never ran"
    for prompt in backend.prompts:
        assert "+VALUE = 2" in prompt
        assert "stack-parent" in prompt
        assert "parent_only.py" not in prompt


def test_find_dispatches_the_review_turn_cap(tmp_path: Path, repo: Path) -> None:
    backend = _FakeBackend()
    pipeline = _pipeline(tmp_path, backend, review_turns=42)
    ref = PullRequestRef(
        number=42, head_ref="fb/child", head_sha="abcdef123456", state="OPEN", base_ref="main"
    )
    pipeline.find(repo, ref)  # type: ignore[arg-type]
    assert backend.turns == [42]


def test_fixer_dispatches_the_fix_turn_cap(tmp_path: Path) -> None:
    backend = _FakeBackend()
    pipeline = _pipeline(tmp_path, backend, fix_turns=7)
    pipeline._run_fixer("fix it", model="m", cwd=tmp_path)
    assert backend.turns == [7]


# ---------------------------------------------------------------------------
# Config keys
# ---------------------------------------------------------------------------


def test_new_keys_have_the_documented_defaults() -> None:
    cfg = load_gate_config({})
    assert cfg is not None
    assert cfg.diff_chars == 150000
    assert cfg.review_turns == 60
    assert cfg.fix_turns == 120


def test_new_keys_are_read_from_config() -> None:
    cfg = load_gate_config({"gate": {"diff_chars": 1000, "review_turns": 5, "fix_turns": 9}})
    assert cfg is not None
    assert (cfg.diff_chars, cfg.review_turns, cfg.fix_turns) == (1000, 5, 9)


def test_verifier_prompt_also_names_the_prs_base(repo: Path, tmp_path: Path) -> None:
    """The verifier writes a test against the same change the reviewer read."""
    backend = _FakeBackend()
    pipeline = _pipeline(tmp_path, backend, base_branch="main")
    pipeline.base_ref = "stack-parent"
    captured: list[str] = []

    class _Capturing(_FakeBackend):
        def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
            captured.append(prompt)
            return _FakeResult(
                json.dumps({"verdict": "REJECTED", "test_file": None, "reason": "no"})
            )

    pipeline.backend = _Capturing()  # type: ignore[assignment]
    from agent_fleet.contracts.gate import Finding

    pipeline._verify_one(
        Finding(id="c-1", file="agent.py", line=1, claim="wrong", repro="x -> y"),
        worktree=repo,
        runner=pipeline._runner_for(repo),
        model="m",
        source="lens",
    )
    assert captured and "stack-parent" in captured[0]
    assert backend.prompts == []


# ---------------------------------------------------------------------------
# The recheck: a stacked PR is rechecked against its own base too
# ---------------------------------------------------------------------------


def test_recheck_resolves_the_prs_own_base_before_merging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merged-tree check must merge the branch the PR will really land on."""
    from agent_fleet.gate import pipeline as pipeline_mod

    seen: dict[str, Any] = {}
    ref = PullRequestRef(
        number=42,
        head_ref="fb/child",
        head_sha="a" * 40,
        state="OPEN",
        base_ref="stack-parent",
    )
    monkeypatch.setattr(pipeline_mod, "resolve_pull_request", lambda *_a, **_k: ref)
    monkeypatch.setattr(pipeline_mod, "fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pipeline_mod,
        "prepare_worktree",
        lambda _repo, path, _sha: seen.setdefault("wt", path),
    )
    monkeypatch.setattr(pipeline_mod, "remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pipeline_mod, "merge_base_into", lambda _wt, base: seen.setdefault("merged", base)
    )
    monkeypatch.setattr(pipeline_mod, "changed_test_files", lambda *_a, **_k: [])
    monkeypatch.setattr(GateTestRunner, "run", lambda _self, _files: TestRun())

    result = pipeline_mod.run_gate_recheck(
        repo_path=tmp_path / "repo",
        pr_number=42,
        approved_sha="",
        head_sha="b" * 40,
        gate_dir=tmp_path / "gate",
    )
    assert isinstance(result, pipeline_mod.GateResult)
    assert seen.get("merged") == "stack-parent"


def test_recheck_falls_back_to_config_when_the_forge_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable forge must not turn a recheck into a failure."""
    from agent_fleet.gate import pipeline as pipeline_mod

    def _boom(*_a: Any, **_k: Any) -> Any:  # noqa: ANN401
        raise pipeline_mod.GateError("github unreachable")

    monkeypatch.setattr(pipeline_mod, "resolve_pull_request", _boom)
    monkeypatch.setattr(pipeline_mod, "fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_mod, "prepare_worktree", lambda _repo, path, _sha: path)
    monkeypatch.setattr(pipeline_mod, "remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_mod, "changed_test_files", lambda *_a, **_k: [])
    monkeypatch.setattr(GateTestRunner, "run", lambda _self, _files: TestRun())

    result = pipeline_mod.run_gate_recheck(
        repo_path=tmp_path / "repo",
        pr_number=42,
        approved_sha="",
        head_sha="b" * 40,
        gate_dir=tmp_path / "gate",
    )
    # No approved sha, so the verdict is a refusal either way — what matters is
    # that it is a verdict about the rebase, not an error about the network.
    assert isinstance(result, pipeline_mod.GateResult)
    assert "github unreachable" not in " ".join(result.reasons)
