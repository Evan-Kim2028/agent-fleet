"""Per-role gate backends: find/judge on OpenRouter, verify/fix on cmd.

The gate's four roles are routed by ``gate.roles``. This file is the acceptance
check for that routing and for the prompt-inlining that lets a backend with no
repo tools review a change at all:

* config — per-role parsing, and the backward-compatible fallback to the single
  ``backend``/``judge_backend`` keys;
* policy — an ``openrouter`` backend restricted to ``[find, judge]`` is enforced
  before dispatch, and a role outside that list fails fast;
* dispatch — each role reaches the backend its config names (mocked backends, so
  this asserts routing, not model output);
* inlining — the diff and the changed non-test files land in the prompt, test
  files do not, the caps hold, and an omission is stated rather than hidden.

The live smoke test at the bottom is opt-in and spends a real OpenRouter call; it
is skipped unless ``AGENT_FLEET_LIVE_OPENROUTER_GATE=1``.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.contracts.gate import Finding
from agent_fleet.gate.config import GateConfig, RoleTarget, load_gate_config
from agent_fleet.gate.gitops import PullRequestRef
from agent_fleet.gate.inline import build_review_context
from agent_fleet.gate.pipeline import (
    GateInfraError,
    GatePipeline,
    _build_role_backends,
    build_gate_backend,
)
from agent_fleet.gate.prompts import find_prompt
from agent_fleet.gate.structured import StructuredCallError, call_structured
from agent_fleet.model_policy import ModelPolicyError, parse_model_policy
from agent_fleet.slots import (
    DEFAULT_OPENROUTER_POOL_SIZE,
    PoolConfig,
    agent_slot_pool,
    openrouter_slot_pool,
)

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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeBackend:
    """Replays a scripted answer; records every prompt and model it was given."""

    answers: dict[str, str] = field(default_factory=dict)
    default: str = ""
    prompts: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    name: str = "fake"
    stderr: str = ""
    exit_code: int = 0

    def run(self, prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(prompt)
        self.models.append(str(kwargs.get("model", "")))
        if self.exit_code != 0:
            return _FakeResult(stdout="", stderr=self.stderr, exit_code=self.exit_code)
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


def _findings_json(*ids: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "id": fid,
                    "file": "src/calc.py",
                    "line": 7,
                    "claim": f"defect {fid}",
                    "repro": "input -> wrong",
                    "testable": True,
                }
                for fid in ids
            ]
        }
    )


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny git repo with one changed source file and one changed test file."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    # ``-b main`` so the fixture's base branch matches the config's default
    # ``base_branch`` regardless of the machine's git default-branch setting.
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "def test_add():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_calc.py").write_text(
        "def test_add():\n    assert True\n\n\ndef test_mul():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    # The gate reviews a PR *branch* against its base, so the change under review
    # must live off ``main`` — otherwise ``main...HEAD`` is empty, as it is at
    # the base commit itself.
    _git(repo, "checkout", "-q", "-b", "feat")
    # The change under review introduces a REAL regression: ``mul`` is added
    # here and returns a + b. A correct lens must flag it, so the live test
    # proves discrimination rather than a blanket "everything is fine".
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n\n"
        "def mul(a, b):\n    return a * b\n\n\n"
        "def div(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_calc.py").write_text(
        "def test_add():\n    assert True\n\n\n"
        "def test_mul():\n    assert True\n\n\n"
        "def test_div():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "branch change")
    return repo


# ---------------------------------------------------------------------------
# Config: per-role parsing and the backward-compatible fallback
# ---------------------------------------------------------------------------


def test_per_role_targets_are_parsed() -> None:
    cfg = load_gate_config(
        {
            "gate": {
                "backend": "cmd",
                "model": SPACE_BUNNY,
                "roles": {
                    "find": {"backend": "openrouter", "model": SPACE_BUNNY},
                    "judge": {"backend": "openrouter", "model": SPACE_BUNNY},
                    "verify": {"backend": "cmd", "model": SPACE_BUNNY},
                    "fix": {"backend": "cmd", "model": SPACE_BUNNY},
                },
            }
        }
    )
    assert cfg is not None
    assert cfg.role_target("find") == RoleTarget("openrouter", SPACE_BUNNY)
    assert cfg.role_target("judge") == RoleTarget("openrouter", SPACE_BUNNY)
    # verify/fix stay on cmd and need repo tools.
    assert cfg.role_target("verify").backend == "cmd"
    assert cfg.role_target("fix").backend == "cmd"


def test_absent_roles_fall_back_to_the_single_backend_keys() -> None:
    """The pre-existing config form must resolve exactly as it did before."""
    cfg = load_gate_config(
        {"gate": {"backend": "grok", "model": "step-5-preview", "judge_backend": "cmd"}}
    )
    assert cfg is not None
    assert cfg.role_target("find") == RoleTarget("grok", "step-5-preview")
    assert cfg.role_target("verify") == RoleTarget("grok", "step-5-preview")
    assert cfg.role_target("fix") == RoleTarget("grok", "step-5-preview")
    # judge always fell back to its own pair, not to gate.backend.
    assert cfg.role_target("judge") == RoleTarget("cmd", None)


def test_defaults_resolve_to_cmd_for_every_role() -> None:
    cfg = GateConfig()
    assert cfg.role_target("find").backend == "cmd"
    assert cfg.role_target("judge").backend == "cmd"
    assert cfg.role_target("verify").backend == "cmd"
    assert cfg.role_target("fix").backend == "cmd"


def test_role_entry_without_a_backend_falls_back_rather_than_emptying() -> None:
    """A half-written entry must not resolve to an unroutable empty backend."""
    cfg = load_gate_config(
        {"gate": {"backend": "cmd", "model": SPACE_BUNNY, "roles": {"find": {"model": "x"}}}}
    )
    assert cfg is not None
    assert cfg.role_target("find") == RoleTarget("cmd", SPACE_BUNNY)


def test_role_without_its_own_model_inherits_the_fallback_model() -> None:
    cfg = load_gate_config(
        {
            "gate": {
                "backend": "cmd",
                "model": SPACE_BUNNY,
                "roles": {"find": {"backend": "openrouter"}},
            }
        }
    )
    assert cfg is not None
    assert cfg.role_target("find") == RoleTarget("openrouter", SPACE_BUNNY)


def test_only_openrouter_gets_the_change_inlined() -> None:
    """cmd reviews keep their shell; only the no-tool backend is inlined."""
    assert RoleTarget("openrouter", SPACE_BUNNY).needs_inline_context is True
    assert RoleTarget("cmd", SPACE_BUNNY).needs_inline_context is False


def test_role_and_slot_knobs_parse() -> None:
    cfg = load_gate_config(
        {
            "gate": {
                "openrouter_slots": 40,
                "inline_diff_chars": 1_000,
                "inline_file_chars": 500,
                "inline_total_chars": 2_000,
            }
        }
    )
    assert cfg is not None
    assert cfg.openrouter_slots == 40
    assert (cfg.inline_diff_chars, cfg.inline_file_chars, cfg.inline_total_chars) == (
        1_000,
        500,
        2_000,
    )


# ---------------------------------------------------------------------------
# Policy: enforced before dispatch
# ---------------------------------------------------------------------------


def test_openrouter_policy_allows_find_and_judge() -> None:
    policy = parse_model_policy(_POLICY_SECTION)
    assert policy.check(backend="openrouter", model=SPACE_BUNNY, role="lens", aliases=("find",))
    assert policy.check(backend="openrouter", model=SPACE_BUNNY, role="judge")


def test_openrouter_policy_refuses_verify_and_fix() -> None:
    """verify/fix need repo tools; the policy must not let them onto openrouter."""
    policy = parse_model_policy(_POLICY_SECTION)
    with pytest.raises(ModelPolicyError, match="may not serve role"):
        policy.check(backend="openrouter", model=SPACE_BUNNY, role="verifier", aliases=("verify",))
    with pytest.raises(ModelPolicyError, match="may not serve role"):
        policy.check(backend="openrouter", model=SPACE_BUNNY, role="fix", aliases=("fix",))


def test_policy_written_with_lens_still_covers_the_find_role() -> None:
    """The existing vocabulary keeps working: ``lens`` and ``find`` are one role."""
    policy = parse_model_policy(
        {
            "model_policy": {
                "backends": {"openrouter": {"allowed_models": [SPACE_BUNNY], "roles": ["lens"]}}
            }
        }
    )
    assert policy.check(backend="openrouter", model=SPACE_BUNNY, role="lens", aliases=("find",))


def test_unlisted_model_still_fails() -> None:
    policy = parse_model_policy(_POLICY_SECTION)
    with pytest.raises(ModelPolicyError, match="not allowed"):
        policy.check(backend="openrouter", model="some/other-model", role="judge")


def test_pre_dispatch_check_rejects_openrouter_for_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole-run check must fail before any backend is built."""
    built: list[str] = []

    def _fake_build(name: str) -> _FakeBackend:
        built.append(name)
        return _FakeBackend(name=name)

    monkeypatch.setattr("agent_fleet.gate.pipeline.build_gate_backend", _fake_build)
    raw = {
        **_POLICY_SECTION,
        "gate": {
            "backend": "cmd",
            "model": SPACE_BUNNY,
            "judge_backend": "cmd",
            "judge_model": SPACE_BUNNY,
            "roles": {
                "find": {"backend": "openrouter", "model": SPACE_BUNNY},
                "verify": {"backend": "openrouter", "model": SPACE_BUNNY},
            },
        },
    }
    cfg = load_gate_config(raw)
    assert cfg is not None
    with pytest.raises(ModelPolicyError, match="may not serve role 'verifier'"):
        _build_role_backends(cfg, parse_model_policy(raw))
    # Every role is validated before the first backend is constructed, so a
    # violation costs a second rather than a fan-out.
    assert built == []


def test_pre_dispatch_check_builds_one_backend_per_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Four roles over two backends must not open four backend sessions."""
    built: list[str] = []
    monkeypatch.setattr(
        "agent_fleet.gate.pipeline.build_gate_backend",
        lambda name: built.append(name) or _FakeBackend(name=name),
    )
    raw = {
        **_POLICY_SECTION,
        "gate": {
            "backend": "cmd",
            "model": SPACE_BUNNY,
            "roles": {
                "find": {"backend": "openrouter", "model": SPACE_BUNNY},
                "judge": {"backend": "openrouter", "model": SPACE_BUNNY},
            },
        },
    }
    cfg = load_gate_config(raw)
    assert cfg is not None
    role_backends = _build_role_backends(cfg, parse_model_policy(raw))
    assert built == ["openrouter", "cmd"]
    # find and judge share the one openrouter instance; verify/fix share cmd.
    assert role_backends["find"] is role_backends["judge"]
    assert role_backends["verify"] is role_backends["fix"]


# ---------------------------------------------------------------------------
# Dispatch: each role reaches the backend its config names
# ---------------------------------------------------------------------------


def _pipeline(
    tmp_path: Path,
    repo: Path,
    *,
    config: GateConfig,
    cmd: _FakeBackend,
    remote: _FakeBackend,
) -> GatePipeline:
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
    # Mirror what ``run_gate`` builds: one instance per distinct backend name,
    # routed by the config's own per-role targets.
    role_backends: dict[str, Any] = {}
    by_name = {"cmd": cmd, "openrouter": remote}
    for role in ("find", "judge", "verify", "fix"):
        role_backends[role] = by_name[config.role_target(role).backend]
    pipe._role_backends = role_backends
    return pipe


def _roles_config(**overrides: Any) -> GateConfig:  # noqa: ANN401
    raw: dict[str, Any] = {
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
    raw.update(overrides)
    cfg = load_gate_config({"gate": raw})
    assert cfg is not None
    return cfg


def test_find_runs_on_the_openrouter_backend(tmp_path: Path, fixture_repo: Path) -> None:

    cmd = _FakeBackend(name="cmd")
    remote = _FakeBackend(answers={"ONE focus": _findings_json("r-1")}, name="openrouter")
    pipe = _pipeline(tmp_path, fixture_repo, config=_roles_config(), cmd=cmd, remote=remote)

    ref = PullRequestRef(number=7, head_ref="feat", head_sha="a" * 40, state="OPEN")
    findings = pipe.find(fixture_repo, ref)

    assert [f.id for f in findings] == ["r-1"]
    assert len(remote.prompts) == 1
    # The lens ran remotely, not on the cmd backend.
    assert cmd.prompts == []
    assert remote.models == [SPACE_BUNNY]


def test_find_prompt_carries_the_inlined_change_not_a_git_instruction(
    tmp_path: Path, fixture_repo: Path
) -> None:

    cmd = _FakeBackend(name="cmd")
    remote = _FakeBackend(default=_findings_json(), name="openrouter")
    pipe = _pipeline(tmp_path, fixture_repo, config=_roles_config(), cmd=cmd, remote=remote)

    ref = PullRequestRef(number=7, head_ref="feat", head_sha="a" * 40, state="OPEN")
    pipe.find(fixture_repo, ref)

    prompt = remote.prompts[0]
    assert "INLINED CHANGE" in prompt
    # The change itself is present: the diff plus the post-change source file.
    assert "def mul(a, b):" in prompt
    assert "src/calc.py" in prompt
    # ...and the lens is never *instructed* to fetch it itself. (The literal
    # "git diff" still appears inside the pasted diff's own `diff --git`
    # headers; what must be absent is the command as a directive.)
    assert "run it" not in prompt
    assert "`git diff" not in prompt
    assert "NO tools, NO shell" in prompt


def test_cmd_find_prompt_keeps_the_git_instruction(tmp_path: Path, fixture_repo: Path) -> None:
    """A tool-capable backend still gets exactly the wording it had before."""

    cmd = _FakeBackend(default=_findings_json(), name="cmd")
    pipe = _pipeline(
        tmp_path,
        fixture_repo,
        config=_roles_config(roles={}),
        cmd=cmd,
        remote=_FakeBackend(name="openrouter"),
    )
    ref = PullRequestRef(number=7, head_ref="feat", head_sha="a" * 40, state="OPEN")
    pipe.find(fixture_repo, ref)

    prompt = cmd.prompts[0]
    assert "git diff" in prompt
    assert "INLINED CHANGE" not in prompt


def test_judge_runs_on_the_openrouter_backend(tmp_path: Path, fixture_repo: Path) -> None:

    cmd = _FakeBackend(name="cmd")
    remote = _FakeBackend(
        answers={
            "final pre-merge judge": json.dumps({"untestable_rulings": [], "new_blockers": []})
        },
        name="openrouter",
    )
    pipe = _pipeline(tmp_path, fixture_repo, config=_roles_config(), cmd=cmd, remote=remote)

    ref = PullRequestRef(number=7, head_ref="feat", head_sha="a" * 40, state="OPEN")
    pipe.judge(fixture_repo, ref)

    assert len(remote.prompts) == 1
    assert cmd.prompts == []
    judge_prompt_sent = remote.prompts[0]
    assert "INLINED CHANGE" in judge_prompt_sent
    assert "def mul(a, b):" in judge_prompt_sent
    assert "`git diff" not in judge_prompt_sent
    assert "NO tools, NO shell" in judge_prompt_sent


def test_verify_stays_on_the_cmd_backend(tmp_path: Path, fixture_repo: Path) -> None:
    """verify must keep the backend that can write a test file."""
    cmd = _FakeBackend(
        answers={
            "PROVE or DISPROVE": json.dumps(
                {"verdict": "REJECTED", "test_file": None, "reason": "x"}
            )
        },
        name="cmd",
    )
    remote = _FakeBackend(name="openrouter")
    pipe = _pipeline(tmp_path, fixture_repo, config=_roles_config(), cmd=cmd, remote=remote)

    finding = Finding(
        id="c-1", file="src/calc.py", line=7, claim="bad", repro="in -> wrong", testable=True
    )
    pipe.verify(fixture_repo, [finding], source="lens")

    assert len(cmd.prompts) == 1
    assert remote.prompts == []
    assert cmd.models == [SPACE_BUNNY]


def test_openrouter_roles_draw_from_the_openrouter_pool_only(tmp_path: Path) -> None:
    """A remote role must not consume or wait on the local cmd agent budget."""
    cfg = _roles_config()
    cmd = _FakeBackend(name="cmd")
    remote = _FakeBackend(name="openrouter")
    pipe = _pipeline(tmp_path, tmp_path, config=cfg, cmd=cmd, remote=remote)
    pool_cfg = PoolConfig(root=tmp_path / "slots", agent_slots=3)
    agent_pool = agent_slot_pool(pool_cfg)
    remote_pool = openrouter_slot_pool(pool_cfg)
    pipe.agent_pool = agent_pool
    pipe.openrouter_pool = remote_pool

    assert pipe._pool_for("find") is remote_pool
    assert pipe._pool_for("judge") is remote_pool
    assert pipe._pool_for("verify") is agent_pool
    assert pipe._pool_for("fix") is agent_pool


def test_openrouter_pool_default_size_is_its_own(tmp_path: Path) -> None:
    pool = openrouter_slot_pool(PoolConfig(root=tmp_path / "slots"))
    assert pool.name == "openrouter"
    assert pool.size == DEFAULT_OPENROUTER_POOL_SIZE == 32
    # Independent of the agent budget: an undeclared openrouter pool is 32 even
    # when the agent pool is configured small.
    small = PoolConfig(root=tmp_path / "slots2", agent_slots=4)
    assert openrouter_slot_pool(small).size == 32


# ---------------------------------------------------------------------------
# Inlining: the evidence a no-tool reviewer actually sees
# ---------------------------------------------------------------------------


def test_context_carries_the_diff_and_the_changed_source(fixture_repo: Path) -> None:
    ctx = build_review_context(fixture_repo, "main")
    assert "def mul(a, b):" in ctx.diff
    assert "def mul(a, b):" in ctx.render()
    assert [f.path for f in ctx.files] == ["src/calc.py"]
    assert "def add(a, b):" in ctx.files[0].text


def test_context_excludes_changed_test_files(fixture_repo: Path) -> None:
    """Test *content* is not inlined; the gate runs those tests instead.

    The test file's diff is still present — a review of a change is not a review
    of half a change — but its full post-change body is not pasted, and the
    omission is stated so the reviewer knows its view is scoped.
    """
    ctx = build_review_context(fixture_repo, "main")
    assert not [f for f in ctx.files if f.path.startswith("tests/")]
    assert ctx.skipped_test_files == ("tests/test_calc.py",)
    # No full-body FILE block for the test...
    assert "----- FILE: tests/test_calc.py -----" not in ctx.render()
    # ...but the diff still shows what changed, and the omission is declared.
    assert "test_div" in ctx.diff
    assert "test_calc.py" in ctx.render()


def test_context_caps_the_diff_and_says_so(fixture_repo: Path) -> None:
    ctx = build_review_context(fixture_repo, "main", max_diff_chars=120)
    assert ctx.diff_truncated is True
    assert "[diff truncated]" in ctx.render()
    assert len(ctx.diff) <= 120


def test_context_drops_whole_files_rather_than_serving_stubs(fixture_repo: Path) -> None:
    """A 200-char head of a file reads as a whole file and invites phantom findings."""
    (fixture_repo / "src" / "other.py").write_text("x = 1\n" * 500, encoding="utf-8")
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-m", "more")

    ctx = build_review_context(fixture_repo, "main", max_file_chars=100, max_total_chars=260)
    assert ctx.omitted >= 1
    assert not [f for f in ctx.files if f.path == "src/other.py"]
    # Any file that WAS inlined is marked when itself truncated.
    assert all(f.truncated or len(f.text) <= 100 for f in ctx.files)
    assert "omitted by the size cap" in ctx.render()


def test_empty_context_renders_a_clear_placeholder(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    ctx = build_review_context(repo, "main")
    assert ctx.is_empty() is True
    assert ctx.render() == "(no change detected)"


def test_find_prompt_without_inlining_is_unchanged_in_shape() -> None:
    prompt = find_prompt(
        lens="correctness",
        focus="f",
        worktree="/tmp/wt",
        base_branch="origin/main",
        head_sha="abc123456",
        pr_number=3,
        task_text="t",
    )
    assert "git diff origin/main...HEAD" in prompt
    assert "INLINED CHANGE" not in prompt


# ---------------------------------------------------------------------------
# Fail-closed: a dead remote backend never reads as a clean PR
# ---------------------------------------------------------------------------


def test_dead_openrouter_lens_fails_the_run_closed(fixture_repo: Path) -> None:
    """A missing API key must read as DEAD, not as a clean review."""
    dead = _FakeBackend(default="", stderr="OPENROUTER_API_KEY is not set", exit_code=1)
    pipe = GatePipeline(
        repo=fixture_repo,
        pr_number=7,
        config=_roles_config(),
        policy=parse_model_policy(_POLICY_SECTION),
        backend=dead,
        judge_backend=dead,
        gate_dir=fixture_repo.parent / "gate",
        use_systemd=False,
    )
    pipe._role_backends = {"find": dead, "judge": dead, "verify": dead, "fix": dead}
    ref = PullRequestRef(number=7, head_ref="feat", head_sha="a" * 40, state="OPEN")

    # A dead reviewer is not evidence of a clean PR.
    with pytest.raises(GateInfraError, match="fail-closed"):
        pipe.find(fixture_repo, ref)
    assert dead.prompts, "the remote backend must actually have been called"


def test_invalid_openrouter_answer_is_classified_invalid() -> None:
    """An unparseable remote answer is 'invalid', which the gate rejects, not 'clean'."""
    with pytest.raises(StructuredCallError) as exc:
        call_structured(
            _FakeBackend(default="I could not review the change."),
            "prompt",
            model=SPACE_BUNNY,
            cwd=Path(),
            timeout_s=5,
            validate=lambda _payload: None,
        )
    assert exc.value.kind == "invalid"


# ---------------------------------------------------------------------------
# Live smoke test — one real lens over a tiny fixture repo
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AGENT_FLEET_LIVE_OPENROUTER_GATE") != "1",
    reason="live OpenRouter call; set AGENT_FLEET_LIVE_OPENROUTER_GATE=1 to run",
)
def test_live_single_lens_over_a_fixture_repo(fixture_repo: Path) -> None:
    """One real OpenRouter lens call against a real two-commit fixture.

    Proves the whole seam end to end: a real model, given the change *inlined*
    rather than told to run git, must find the regression the branch
    introduces (``div`` returns a + b) and name the file.
    """
    backend = build_gate_backend("openrouter")
    cmd = _FakeBackend(name="cmd")
    pipe = _pipeline(
        fixture_repo,
        fixture_repo,
        config=_roles_config(),
        cmd=cmd,
        remote=backend,  # type: ignore[arg-type]
    )
    ref = PullRequestRef(
        number=7,
        head_ref="feat",
        head_sha=_git(fixture_repo, "rev-parse", "HEAD").strip(),
        state="OPEN",
    )
    findings = pipe.find(fixture_repo, ref)
    assert any("calc.py" in f.file for f in findings), [f.to_dict() for f in findings]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
