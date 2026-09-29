"""Follow-up: the PR's own base is used at *every* diff site, not most of them.

The reviewer-brief PR resolved the base once, into ``pipeline.base_ref``, and
routed the reviewers, the verifiers, the judge, step0, the patch-id carry-over
and the merged-tree regression check through it. The tiering and recheck sites
were left reading ``config.base_branch`` — the configured default, i.e. main —
so a stacked PR was tiered, approved and rechecked on its parent branch's
commits rather than its own.

Each test builds a real stack (``main`` -> ``parent`` -> ``child``) and asserts
the site under test sees only the child's diff. A base that resolves but is the
wrong one is silent: the reviewer sees a plausible file list, the tier is a
defensible-looking verdict, the recheck returns a verdict. Only the diff against
the PR's own base tells the truth.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.gate import pipeline as pipeline_mod
from agent_fleet.gate import standard
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.gitops import PullRequestRef
from agent_fleet.gate.pipeline import GatePipeline, GateTestRunner, TestRun
from agent_fleet.model_policy import ModelPolicy

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "gate-stack-fixture"
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
def stack(tmp_path: Path) -> Path:
    """main -> stack-parent (tier-defining product code) -> stack-child (markdown).

    The child is the PR under test: one markdown file, on a base that already
    carries more code than the tier thresholds. Diffed against main the child
    carries its parent's product files; against its own base it is one
    documentation line. Nothing about the child is ambiguous — only the base is.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "base.py").write_text("BASE = 1\n", encoding="utf-8")
    _commit(root, "base")

    _git(root, "checkout", "-q", "-b", "stack-parent")
    (root / "big.py").write_text("".join(f"L{i} = {i}\n" for i in range(5000)), encoding="utf-8")
    # A test on the base branch, not this PR's, so a stacked child is shown
    # inheriting a test suite it never wrote.
    (root / "tests").mkdir()
    (root / "tests" / "test_parent.py").write_text("def t():\n    pass\n", encoding="utf-8")
    _commit(root, "parent work")

    _git(root, "checkout", "-q", "-b", "stack-child")
    (root / "notes.md").write_text("# notes\n", encoding="utf-8")
    _commit(root, "child work")
    return root


@dataclass
class _FakeBackend:
    answer: str = '```json\n{"findings": []}\n```'

    def run(self, _prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        return _Result(self.answer)


@dataclass(frozen=True)
class _Result:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _pipeline(tmp_path: Path, repo: Path, **overrides: Any) -> GatePipeline:  # noqa: ANN401
    base: dict[str, Any] = {
        "backend": "cmd",
        "model": "m",
        "base_branch": "main",
        "enable_judge": False,
        "enable_fix": False,
        "lenses": ("correctness",),
    }
    base.update(overrides)
    pipeline = GatePipeline(
        repo=repo,
        pr_number=42,
        config=GateConfig(**base),
        policy=ModelPolicy(backends={}),
        backend=_FakeBackend(),  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )
    # What run() resolves from the forge before any stage diffs anything.
    pipeline.base_ref = "stack-parent"
    return pipeline


# ---------------------------------------------------------------------------
# all-1: STANDARD-tier selection
# ---------------------------------------------------------------------------


def test_standard_tier_selection_sees_only_the_childs_own_diff(stack: Path, tmp_path: Path) -> None:
    """A one-line markdown child takes the STANDARD path; the parent's code is not in it.

    ``select_tier`` is a single sensitive-path veto over the file list, so the
    base it is handed decides which review path the PR pays for. Against main the
    parent's ``big.py`` is in that list and the child is sent down the full
    evidence gate for code it never wrote.
    """
    pipeline = _pipeline(tmp_path, stack, sensitive_paths=("big.py",))
    assert pipeline.config.base_branch == "main"
    assert pipeline.base_ref == "stack-parent"

    against_parent = pipeline_mod.changed_paths(stack, pipeline.base_ref)
    against_main = pipeline_mod.changed_paths(stack, pipeline.config.base_branch)
    # The fixture is only meaningful if main really does show the parent's work.
    assert "big.py" in against_main
    assert against_parent == ["notes.md"]

    assert standard.select_tier(pipeline.config, against_parent) == standard.STANDARD_TIER
    assert standard.select_tier(pipeline.config, against_main) == standard.SENSITIVE_TIER


def test_run_selects_the_standard_tier_against_the_resolved_base(
    stack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run()`` must hand select_tier the child's diff, not main's.

    Asserting on ``select_tier`` in isolation would pass even with the call site
    still reading ``config.base_branch`` — the call site is the defect, so the
    file list that reaches it is what is checked here.
    """
    pipeline = _pipeline(tmp_path, stack, sensitive_paths=("big.py",))
    seen: list[list[str]] = []

    def _record(config: GateConfig, changed: list[str]) -> str:
        seen.append(list(changed))
        return standard.select_tier(config, changed)

    ref = PullRequestRef(
        number=42,
        head_ref="stack-child",
        head_sha="d" * 40,
        state="OPEN",
        base_ref="stack-parent",
    )
    # A gate worktree is detached at the PR head, so the stack's own branches are
    # unreachable by name there; the real thing is a merge of origin/<base> and
    # the head, which is what a reviewer sees and what the diff is taken against.
    # It goes where the gate puts its own: ``gate_dir / "wt"``.
    worktree = tmp_path / "gate" / "wt"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(stack, "worktree", "add", "-q", "--detach", str(worktree), "stack-child")
    _git(worktree, "merge", "-q", "stack-parent", "-m", "merge base")
    monkeypatch.setattr(pipeline_mod, "resolve_pull_request", lambda *_a, **_k: ref)
    monkeypatch.setattr(pipeline_mod, "fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr(pipeline_mod, "prepare_worktree", lambda _r, _path, _s: _path)
    monkeypatch.setattr(GatePipeline, "run_pr_tests", lambda _self, _wt: [])
    monkeypatch.setattr(GatePipeline, "tier0_eligible", lambda _self, _wt: [])
    monkeypatch.setattr(GatePipeline, "tier0_evidence_gap", lambda _self, _wt: [])
    monkeypatch.setattr(
        GatePipeline,
        "run_standard",
        lambda _self, *_a, **_k: pipeline._finish(
            pipeline_mod.GateOutcome.APPROVED, ref.head_sha, [], ref
        ),
    )
    monkeypatch.setattr(
        GatePipeline,
        "review_tier",
        lambda _self, _wt: pytest.fail("the full evidence gate was entered for a docs-only diff"),
    )
    monkeypatch.setattr(pipeline_mod, "standard_select_tier", _record)

    result = pipeline.run()

    assert seen, "the STANDARD tier was never selected"
    assert seen[0] == ["notes.md"], f"tiered against the wrong base: {seen[0]}"
    assert isinstance(result, pipeline_mod.GateResult)


def test_review_tier_sizes_the_pr_against_its_own_base(stack: Path, tmp_path: Path) -> None:
    """Tier 1 vs the full lens set is decided by the PR's diff, not the stack's.

    The same wrong base one stage later: ``diff_line_stats`` and
    ``prodsensitive_paths`` both read ``config.base_branch``, so a stacked child
    is counted at its parent's size and escalated to every configured lens.
    """
    pipeline = _pipeline(tmp_path, stack, big_lines=1200, prodsensitive_paths=("big.py",))

    against_parent = pipeline.review_tier(stack)
    assert against_parent.tier == 1
    assert against_parent.lines == 0  # the child adds a markdown file, not code

    # What the old site read: the parent's 5000 lines, over big_lines, and a
    # production-sensitive path that is not this PR's — the full lens set.
    pipeline.base_ref = "main"
    against_main = pipeline.review_tier(stack)
    assert against_main.tier == len(pipeline.config.lenses)
    assert against_main.lines > 1200
    assert against_main.risky == ["big.py"]


# ---------------------------------------------------------------------------
# all-2: tier-0 eligibility
# ---------------------------------------------------------------------------


def test_tier0_eligible_ignores_the_parent_branches_product_code(
    stack: Path, tmp_path: Path
) -> None:
    """A docs-only stacked child that step0 proved green gets its no-model approval.

    The guard is ``any(not is_docs_or_test(path) for path in changed)``: one
    product file in the list refuses tier 0. Diffed against main the parent's
    ``big.py`` is in that list, so the child is escalated for review on the
    strength of commits it does not contain.
    """
    pipeline = _pipeline(tmp_path, stack, tier0=True)
    assert pipeline.config.base_branch == "main"

    changed = pipeline.tier0_eligible(stack)
    assert changed == ["notes.md"], f"tier 0 refused on files the PR never touched: {changed}"

    # The refusal is real, not vacuous: the same PR against the wrong base is
    # refused outright, which is exactly what the defect looked like.
    pipeline.base_ref = "main"
    assert pipeline.tier0_eligible(stack) == []


def test_tier0_evidence_gap_reads_the_childs_own_test_deletions(
    stack: Path, tmp_path: Path
) -> None:
    """Test deletions and suite-config edits are read off the child's own diff.

    A deletion the PR did not make must not escalate it, and a deletion it did
    make must still refuse — the fix is a different base, not a weaker check.
    """
    pipeline = _pipeline(tmp_path, stack, tier0=True)
    # The parent branch's product code and test file are not in the child's diff.
    assert pipeline.tier0_evidence_gap(stack) == []

    # The child's own deletion is a real evidence gap and must still be caught —
    # the fix is a different base, not a weaker check.
    (stack / "tests" / "test_parent.py").unlink()
    _commit(stack, "child deletes the base test")
    assert any("test_parent.py" in reason for reason in pipeline.tier0_evidence_gap(stack))

    # The parent's own test file, read through the wrong base, is a deletion
    # this PR never made and escalates it anyway — the original defect.
    pipeline.base_ref = "main"
    assert pipeline_mod.deleted_test_paths(stack, pipeline.base_ref) == []


# ---------------------------------------------------------------------------
# all-3: the recheck
# ---------------------------------------------------------------------------


def test_recheck_enumerates_the_childs_own_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recheck certifies *this* PR's approval carry-over, so it runs its tests.

    The base merge already resolved to the PR's own base. Enumerating the tests
    against the un-resolved configured branch adds the parent branch's whole test
    suite: a regression on the base branch then fails — or is masked by — a
    recheck whose verdict is only supposed to cover the child's approval.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "gate@test.local")
    _git(repo, "config", "user.name", "Gate Test")
    (repo / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_parent.py").write_text("def t():\n    pass\n", encoding="utf-8")
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "stack-parent")
    (repo / "tests" / "test_parent.py").write_text("def t():\n    assert True\n", encoding="utf-8")
    _commit(repo, "parent touches its test")

    _git(repo, "checkout", "-q", "-b", "stack-child")
    (repo / "tests" / "test_child.py").write_text("def t():\n    pass\n", encoding="utf-8")
    _commit(repo, "child adds its test")

    ref = PullRequestRef(
        number=42,
        head_ref="stack-child",
        head_sha="e" * 40,
        state="OPEN",
        base_ref="stack-parent",
    )
    seen: dict[str, Any] = {}
    real_changed = pipeline_mod.changed_test_files

    def _record(worktree: Path, base: str) -> list[str]:
        seen.setdefault("base", base)
        return real_changed(worktree, base)

    monkeypatch.setattr(pipeline_mod, "resolve_pull_request", lambda *_a, **_k: ref)
    monkeypatch.setattr(pipeline_mod, "fetch_base", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pipeline_mod, "prepare_worktree", lambda _r, path, _s: seen.setdefault("wt", path)
    )
    monkeypatch.setattr(pipeline_mod, "remove_worktree", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pipeline_mod, "merge_base_into", lambda _wt, base: seen.setdefault("merged", base)
    )
    monkeypatch.setattr(pipeline_mod, "changed_test_files", _record)
    monkeypatch.setattr(GateTestRunner, "run", lambda _self, _files: TestRun())

    result = pipeline_mod.run_gate_recheck(
        repo_path=repo,
        pr_number=42,
        approved_sha="",
        head_sha="f" * 40,
        gate_dir=tmp_path / "gate",
    )

    assert isinstance(result, pipeline_mod.GateResult)
    assert seen.get("merged") == "stack-parent"
    # The defect in one line: the merge used the resolved base, the enumeration
    # did not, so one function disagreed with itself about the base.
    assert seen.get("base") == "stack-parent", f"tests enumerated against {seen.get('base')!r}"
