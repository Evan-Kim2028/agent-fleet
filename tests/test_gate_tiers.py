"""Review tiering: how much review a diff earns, decided before any model runs.

The gate used to spend the same fan-out on every PR — four lens reviewers plus a
verifier per claim — which is the wrong price for two very different diffs. A PR
that only rewrites prose and moves test cases has no product behaviour to review;
a PR that touches a migration or a deploy script should never get one reviewer.

The tiers are a cost decision, so these tests pin the decision rather than the
wording: which tier a given diff earns, that the choice is logged with the
numbers behind it, and — most of all — that each *refusal* holds. A tier that
approves too eagerly is the one bug worth writing a test for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any, Protocol

import pytest

from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.gitops import diff_line_stats, is_docs_or_test
from agent_fleet.gate.pipeline import (
    GatePipeline,
    PullRequestRef,
    ReviewTier,
    TestRun,
    tier_fields,
)
from agent_fleet.gate.prompts import ALL_FOCUS, ALL_FOCUS_LENS
from agent_fleet.model_policy import ModelPolicy

# ---------------------------------------------------------------------------
# Fakes — no network, no real model, no real git
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """Replays one answer for every call; records which lenses were asked for."""

    answer: str = json.dumps({"findings": []})
    prompts: list[str] = field(default_factory=list)

    def run(self, prompt: str, **_kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        return _FakeResult(self.answer)


@dataclass(frozen=True)
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


def _ref() -> PullRequestRef:
    return PullRequestRef(
        number=42,
        head_ref="fb/lane",
        head_sha="abc123def4567890",
        state="OPEN",
        base_ref="main",
    )


def _config(**overrides: Any) -> GateConfig:  # noqa: ANN401
    base: dict[str, Any] = {
        "backend": "cmd",
        "model": "m",
        "judge_backend": "cmd",
        "judge_model": "m",
        "enable_judge": False,
        "enable_fix": False,
        "lens_timeout_s": 10,
        "verify_timeout_s": 10,
        "judge_timeout_s": 10,
        "fix_timeout_s": 10,
        "test_timeout_s": 10,
    }
    base.update(overrides)
    return GateConfig(**base)


def _load(section: dict[str, Any]) -> GateConfig:
    """``load_gate_config`` narrowed: a non-empty section never disables the gate."""
    config = load_gate_config({"gate": section})
    assert config is not None
    return config


def _pipeline(tmp_path: Path, backend: _FakeBackend, config: GateConfig) -> GatePipeline:
    return GatePipeline(
        repo=tmp_path / "repo",
        pr_number=42,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=backend,  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )


class _DiffSetter(Protocol):
    """``(changed paths, {path: (added, deleted)}) -> None``.

    A test states the diff it means rather than building a repo to produce one.
    A Protocol rather than a ``Callable`` alias because the line counts are
    optional — a test about *which files* changed does not care about size.
    """

    def __call__(self, paths: list[str], lines: dict[str, tuple[int, int]] | None) -> None: ...


@pytest.fixture
def diff(monkeypatch: pytest.MonkeyPatch) -> _DiffSetter:
    """Stub the two git reads tiering depends on, so no repo is needed."""

    def _set(paths: list[str], lines: dict[str, tuple[int, int]] | None = None) -> None:
        counts = lines or {}
        monkeypatch.setattr(
            "agent_fleet.gate.pipeline.changed_paths",
            lambda *_a, **_k: list(paths),
        )
        monkeypatch.setattr(
            "agent_fleet.gate.pipeline.diff_line_stats",
            lambda *_a, **_k: sum(a + d for a, d in counts.values()),
        )
        monkeypatch.setattr(
            "agent_fleet.gate.gitops.changed_paths",
            lambda *_a, **_k: list(paths),
        )
        monkeypatch.setattr(
            "agent_fleet.gate.gitops.diff_line_stats",
            lambda *_a, **_k: sum(a + d for a, d in counts.values()),
        )

    return _set


# ---------------------------------------------------------------------------
# Tier 0 — docs/tests only, approved on evidence alone
# ---------------------------------------------------------------------------


def test_a_docs_only_pr_is_tier0_eligible(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["README.md", "docs/GATE.md", "tests/test_x.py", "fixtures/a.json"], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    assert pipe.tier0_eligible(tmp_path) == [
        "README.md",
        "docs/GATE.md",
        "tests/test_x.py",
        "fixtures/a.json",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "agent_fleet/gate/pipeline.py",
        "scripts/deploy.sh",
        "api/app.py",
        "src/helper.py",
        "docs_gen/api.py",
    ],
)
def test_a_product_file_refuses_tier0(tmp_path: Path, diff: _DiffSetter, path: str) -> None:
    diff([path], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    assert pipe.tier0_eligible(tmp_path) == []


def test_tier0_is_off_when_configured(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["docs/GATE.md"], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config(tier0=False))
    assert pipe.tier0_eligible(tmp_path) == []


def test_tier0_refuses_when_a_pr_test_fails(tmp_path: Path, diff: _DiffSetter) -> None:
    """The load-bearing refusal: a red test is a blocker, docs-only or not."""
    diff(["tests/test_x.py"], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    pipe._step0_run = TestRun(failing=["tests/test_x.py::test_y"], tests_failed=True)
    pipe.evidence.confirmed.append({"id": "T-test_y", "source": "pr-tests", "claim": "red"})
    assert pipe.tier0_eligible(tmp_path) == []


def test_tier0_refuses_an_empty_diff(tmp_path: Path, diff: _DiffSetter) -> None:
    """A git failure reads as no changed files; that must not become an approval."""
    diff([], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    assert pipe.tier0_eligible(tmp_path) == []


def test_tier0_dispatches_no_reviewer(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["docs/GATE.md"], None)
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, _config())
    assert pipe.tier0_eligible(tmp_path) == ["docs/GATE.md"]
    assert pipe.find(tmp_path, _ref(), ()) == []
    assert backend.prompts == []


# ---------------------------------------------------------------------------
# Tier 1 vs tier 4 — size and production sensitivity
# ---------------------------------------------------------------------------


def test_a_small_safe_diff_gets_one_all_focus_reviewer(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["agent_fleet/foo.py"], {"agent_fleet/foo.py": (20, 5)})
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    tier = pipe.review_tier(tmp_path)
    assert tier.tier == 1
    assert tier.lenses == (ALL_FOCUS_LENS,)


def test_a_big_diff_keeps_the_full_lens_set(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["agent_fleet/foo.py"], {"agent_fleet/foo.py": (1201, 0)})
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    tier = pipe.review_tier(tmp_path)
    assert tier.tier == len(_config().lenses)
    assert set(tier.lenses) == set(_config().lenses)
    assert ALL_FOCUS_LENS not in tier.lenses


def test_the_size_threshold_is_strictly_greater_than(tmp_path: Path, diff: _DiffSetter) -> None:
    """Exactly big_lines is not over it: the threshold is a "> big_lines" test."""
    diff(["agent_fleet/foo.py"], {"agent_fleet/foo.py": (1200, 0)})
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    assert pipe.review_tier(tmp_path).tier == 1


def test_big_lines_is_configurable(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["agent_fleet/foo.py"], {"agent_fleet/foo.py": (60, 0)})
    pipe = _pipeline(tmp_path, _FakeBackend(), _config(big_lines=10))
    assert pipe.review_tier(tmp_path).tier == len(_config().lenses)


@pytest.mark.parametrize(
    "path",
    [
        "infra/vps/thing.sh",
        ".github/workflows/ci.yml",
        "scripts/lor-api-ship",
        "scripts/platform-deploy.sh",
        "db/migrations/0001_x.sql",
        "agent_fleet/sales_publish.py",
        "agent_fleet/gold_backfill.py",
        "scripts/run_prod_thing.py",
    ],
)
def test_a_production_sensitive_path_keeps_the_full_lens_set(
    tmp_path: Path,
    diff: _DiffSetter,
    path: str,
) -> None:
    diff([path], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    tier = pipe.review_tier(tmp_path)
    assert tier.tier == len(_config().lenses)
    assert tier.risky == [path]


def test_a_small_ordinary_diff_is_not_production_sensitive(
    tmp_path: Path, diff: _DiffSetter
) -> None:
    """The patterns must not fire on ordinary product code by accident."""
    diff(["agent_fleet/gate/pipeline.py", "agent_fleet/sales.py"], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    assert pipe.review_tier(tmp_path).risky == []


def test_the_prodsensitive_list_is_configurable(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["migrations/0001_x.sql"], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config(prodsensitive_paths=(r"^db/",)))
    tier = pipe.review_tier(tmp_path)
    assert tier.risky == []
    assert tier.tier == 1


def test_the_tier_counts_the_configured_lens_set(tmp_path: Path, diff: _DiffSetter) -> None:
    """Two configured lenses is still the full-set tier — the number is the count."""
    diff(["infra/vps/x.sh"], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config(lenses=("correctness", "spec")))
    assert pipe.review_tier(tmp_path).tier == 2


# ---------------------------------------------------------------------------
# The log line
# ---------------------------------------------------------------------------


def test_the_tier_is_logged_with_the_numbers_behind_it(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["infra/vps/x.sh", "agent_fleet/foo.py"], {"agent_fleet/foo.py": (30, 2)})
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    summary = pipe.review_tier(tmp_path).summary
    assert "review tier: 4" in summary
    assert "non-test diff 32 lines" in summary
    assert "1 production-sensitive files" in summary


def test_tier_fields_carry_the_summary_and_the_paths(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["infra/vps/x.sh"], None)
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    fields = tier_fields(pipe.review_tier(tmp_path))
    assert fields["tier"] == 4
    assert fields["non_test_lines"] == 0
    assert fields["n_prodsensitive"] == 1
    assert fields["prodsensitive"] == ["infra/vps/x.sh"]
    assert "review tier: 4" in fields["summary"]


def test_the_prodsensitive_list_in_the_log_is_bounded() -> None:
    """A wide PR must not put 400 paths in one log line."""
    tier = ReviewTier(tier=4, lenses=("a",), lines=1, risky=[f"p{i}.py" for i in range(400)])
    fields = tier_fields(tier)
    assert len(fields["prodsensitive"]) == 10
    assert fields["n_prodsensitive"] == 400


def test_a_tier0_log_line_says_zero_and_names_the_evidence() -> None:
    tier = GatePipeline.tier0_tier(["docs/a.md", "tests/test_a.py", "fixtures/f.json"])
    summary = tier_fields(tier)["summary"]
    assert "review tier: 0" in summary
    assert "3 changed file(s), all docs/tests/fixtures" in summary
    assert "no model review" in summary
    assert tier.lenses == ()


# ---------------------------------------------------------------------------
# The all-focus reviewer really covers all four focuses
# ---------------------------------------------------------------------------


def test_the_all_focus_prompt_names_every_focus(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["agent_fleet/foo.py"], {"agent_fleet/foo.py": (5, 0)})
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, _config())
    pipe.find(tmp_path, _ref(), pipe.review_tier(tmp_path).lenses)
    assert len(backend.prompts) == 1
    prompt = backend.prompts[0]
    for focus in ("CORRECTNESS", "CONTRACT", "PRODSAFETY", "SPEC"):
        assert focus in prompt
    assert ALL_FOCUS in prompt


def test_a_tier4_run_asks_each_configured_lens(tmp_path: Path, diff: _DiffSetter) -> None:
    diff(["infra/vps/x.sh"], None)
    backend = _FakeBackend()
    pipe = _pipeline(tmp_path, backend, _config())
    pipe.find(tmp_path, _ref(), pipe.review_tier(tmp_path).lenses)
    assert len(backend.prompts) == len(_config().lenses)


# ---------------------------------------------------------------------------
# The git-side classification, against a real repo
# ---------------------------------------------------------------------------


def _git_env(tmp_path: Path) -> dict[str, str]:
    """A git env with an identity and a writable HOME, so commits work anywhere."""
    return {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }


def _repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env(tmp_path)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "fb/lane")
    return repo, env


def test_a_real_diff_counts_only_non_test_lines(tmp_path: Path) -> None:
    """The counter is the reason most PRs are not "big"; prove it on a real diff."""
    import subprocess

    repo, env = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("\n".join(f"a{i} = {i}" for i in range(200)) + "\n")
    (repo / "snap.json").write_text("{}\n" * 100, encoding="utf-8")
    (repo / "app.py").write_text(
        "\n".join(f"y{i} = {i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    (repo / "docs" / "a.md").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "change"],
        check=True,
        capture_output=True,
        env=env,
    )
    # app.py is rewritten: 20 added + 1 deleted. The 200 test lines, the 100 JSON
    # lines and the one doc line are all excluded.
    assert diff_line_stats(repo, "main") == 21


def test_a_real_docs_only_diff_is_tier0_shaped(tmp_path: Path) -> None:
    """The full tier-0 question, asked of a real worktree rather than a stub."""
    import subprocess

    repo, env = _repo(tmp_path)
    (repo / "docs" / "a.md").write_text("b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "docs+tests"],
        check=True,
        capture_output=True,
        env=env,
    )
    pipe = _pipeline(tmp_path, _FakeBackend(), _config())
    assert sorted(pipe.tier0_eligible(repo)) == ["docs/a.md", "tests/test_a.py"]
    # A product file in the same PR pulls it out of tier 0.
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "plus code"],
        check=True,
        capture_output=True,
        env=env,
    )
    assert pipe.tier0_eligible(repo) == []


def test_is_docs_or_test_covers_the_documented_forms() -> None:
    for path in (
        "README.md",
        "docs/GATE.md",
        "doc/x.md",
        "docs/app.py",
        "tests/test_a.py",
        "api/tests/test_b.py",
        "pkg/foo_test.py",
        "fixtures/data.json",
    ):
        assert is_docs_or_test(path), path
    for path in (
        "app.py",
        "api/docsgen.py",
        "scripts/fixture.py",
        "docs_gen/api.py",
        "testsuite/app.py",
    ):
        assert not is_docs_or_test(path), path


def test_a_non_test_python_file_under_test_is_kept_out_of_the_test_bucket() -> None:
    """``test_*.py`` is the test selector the whole gate already uses (step0), so
    tier 0 agrees with it. A helper that happens to be named ``test_*.py`` is
    caught — deliberately, so the two never disagree about what a test is."""
    assert is_docs_or_test("src/test_helpers.py")
    assert not is_docs_or_test("src/helpers.py")


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_the_tier_defaults() -> None:
    config = load_gate_config({})
    assert config is not None
    assert config.tier0 is True
    assert config.big_lines == 1200
    assert len(config.prodsensitive_paths) == 6


def test_the_tier_keys_load() -> None:
    config = _load({"tier0": False, "big_lines": 42, "prodsensitive_paths": ["^only/"]})
    assert config.tier0 is False
    assert config.big_lines == 42
    assert config.prodsensitive_paths == ("^only/",)
    assert config.is_prodsensitive("only/x.py")
    assert not config.is_prodsensitive("migrations/1.sql")


def test_an_empty_prodsensitive_list_is_honoured() -> None:
    """An empty list means "nothing is sensitive here", not "use the defaults"."""
    config = _load({"prodsensitive_paths": []})
    assert config.prodsensitive_paths == ()
    assert not config.is_prodsensitive("migrations/1.sql")


def test_a_malformed_prodsensitive_list_falls_back_to_the_defaults() -> None:
    config = _load({"prodsensitive_paths": "^oops"})
    assert config.prodsensitive_paths == GateConfig().prodsensitive_paths
