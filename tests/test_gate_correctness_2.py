"""Claim correctness-2: an unresolvable diff range silently becomes a clean review.

``agent_fleet/gate/inline.py`` builds the change-under-review text that is pasted
into prompts for backends with no repo tools (OpenRouter). Its private ``_git``
helper runs ``git`` with ``check=False`` and returns ``done.stdout or ""`` on both
failure paths: a non-zero exit and a failed spawn. It never inspects
``returncode``, so when the diff range cannot be resolved — an orphan branch with
no merge base against the base branch — ``git diff <base>...HEAD`` exits 128 with
empty stdout and the failure is discarded.

The result is a ``ReviewContext`` indistinguishable from a PR that genuinely
changed nothing: empty diff, no files, ``omitted == 0``. ``render()`` returns the
literal placeholder ``"(no change detected)"``, which is a NON-EMPTY string, so
``find_prompt``'s ``if inlined_context:`` branch fires and pastes that placeholder
into the prompt under a header reading "the change under review". A lens with no
tools is told the only code it can see is the change pasted below, and the change
pasted below is a placeholder string. It can only return an empty findings list,
which the gate records as a clean review / APPROVED.

The module docstring of ``inline.py`` states the property being violated: "a
reviewer must never be silently handed a partial view and report it as complete,
because the gate cannot tell a truncated review from a clean one." A diff that
could not be computed at all is the strongest form of that, and ``omitted`` — the
fail-visible channel — stays 0, so nothing downstream marks the review incomplete.

These tests drive the real code: a real orphan git repo, the real
``build_review_context`` / ``ReviewContext.render``, the real ``GatePipeline.find``
routing ``find`` to an OpenRouter-style (no-tool) backend, and the real
``find_prompt``. Only the LLM backend is a recording fake, so the assertion is on
what the pipeline actually hands a reviewer, not on model output.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_fleet.gate.config import load_gate_config
from agent_fleet.gate.gitops import PullRequestRef
from agent_fleet.gate.inline import build_review_context
from agent_fleet.gate.pipeline import GateInfraError, GatePipeline
from agent_fleet.gate.prompts import find_prompt
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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def orphan_repo(tmp_path: Path) -> Path:
    """A PR head with NO merge base against ``main``, carrying a real change.

    ``git checkout --orphan`` gives ``feat`` a root commit disjoint from ``main``,
    so ``git diff main...HEAD`` exits 128 ("no merge base") instead of returning a
    diff. The branch still contains a genuine, reviewable source change — the
    point being that the gate cannot see it.
    """
    repo = tmp_path / "orphan-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main", ".")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "--orphan", "feat")
    _git(repo, "rm", "-q", "-rf", "--cached", ".")
    (repo / "a.txt").unlink()
    (repo / "src").mkdir()
    # A real, defective change the lens should have been shown.
    (repo / "src" / "calc.py").write_text("def div(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orphan change")

    # Guard the premise: the range really is unresolvable, and git really does
    # report it on stderr only, with nothing on stdout to notice.
    probe = _git(repo, "diff", "main...HEAD")
    assert probe.returncode != 0, "fixture must have an unresolvable diff range"
    assert probe.stdout == "", "the failure must be invisible on stdout"

    _git(repo, "checkout", "-q", "feat")
    return repo


@dataclass(frozen=True)
class _FakeResult:
    stdout: str
    stderr: str = ""
    exit_code: int = 0
    duration_s: float = 0.0
    agent_id: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class _RecordingBackend:
    """A no-tool remote backend. Records the prompt; answers with a clean review.

    It answers ``{"findings": []}`` — the *only* honest answer available to a
    reviewer whose only view of the change is ``(no change detected)``. The gate
    must not be able to tell this apart from a genuinely clean review, which is
    the defect.
    """

    name: str = "openrouter"
    prompts: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    def run(self, prompt: str, **kwargs: Any) -> _FakeResult:
        self.prompts.append(prompt)
        self.models.append(str(kwargs.get("model", "")))
        return _FakeResult(json.dumps({"findings": []}))


def _config() -> Any:
    cfg = load_gate_config(
        {
            "gate": {
                "backend": "cmd",
                "model": SPACE_BUNNY,
                "base_branch": "main",
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


def _pipeline(tmp_path: Path, repo: Path, remote: _RecordingBackend) -> GatePipeline:
    pipe = GatePipeline(
        repo=repo,
        pr_number=7,
        config=_config(),
        policy=parse_model_policy(_POLICY_SECTION),
        backend=remote,
        judge_backend=remote,
        gate_dir=tmp_path / "gate",
        use_systemd=False,
    )
    by_name = {"cmd": remote, "openrouter": remote}
    pipe._role_backends = {r: by_name[_config().role_target(r).backend] for r in
                           ("find", "judge", "verify", "fix")}
    return pipe


# ---------------------------------------------------------------------------
# The defect, at the layer where it is born
# ---------------------------------------------------------------------------


def test_unresolvable_diff_range_is_reported_as_an_empty_change(orphan_repo: Path) -> None:
    """``_git`` must not turn a failed ``git diff`` into "no change".

    At head, a non-zero exit is indistinguishable from a PR with no changes: the
    context renders the empty placeholder and ``omitted`` — the fail-visible
    channel for "the reviewer is not seeing the whole change" — stays 0.
    """
    ctx = build_review_context(orphan_repo, "main")

    assert not ctx.is_empty(), (
        "git diff main...HEAD failed (no merge base) on a repo that HAS a change; "
        "the failure was swallowed and reported as an empty change"
    )
    # The branch really does contain changed source; it just never made it in.
    assert ctx.diff, "the diff of a real change came back empty"
    assert [f.path for f in ctx.files] == ["src/calc.py"]
    assert "def div" in ctx.diff


def test_unresolvable_diff_is_fail_visible_in_the_rendered_context(orphan_repo: Path) -> None:
    """Whatever the context renders must be usable as a change under review.

    The placeholder ``(no change detected)`` is a bare string with no evidence in
    it, and it is pasted under a "the change under review" header — so the prompt
    asserts a review happened over text that contains no code.
    """
    rendered = build_review_context(orphan_repo, "main").render()

    assert "no change detected" not in rendered, (
        "an unresolvable diff range rendered as the empty placeholder, which is "
        "indistinguishable from a clean PR and is pasted as if it were the change"
    )


def test_unresolvable_diff_marks_the_review_incomplete(orphan_repo: Path) -> None:
    """A failed diff must set a fail-visible signal, not leave ``omitted`` at 0."""
    ctx = build_review_context(orphan_repo, "main")

    assert ctx.omitted >= 1 or "unavailable" in ctx.render().lower(), (
        "nothing marks this review as incomplete: omitted == 0 and the rendered "
        "text carries no warning, so the gate records a clean review"
    )


# ---------------------------------------------------------------------------
# The defect at the prompt a real no-tool reviewer is handed
# ---------------------------------------------------------------------------


def test_find_prompt_pastes_the_placeholder_as_the_change_under_review(
    orphan_repo: Path,
) -> None:
    """The inlined branch fires on a placeholder and tells the model to trust it."""
    inlined = build_review_context(orphan_repo, "main").render()
    assert inlined, "the placeholder is a non-empty string, so the branch can fire"

    prompt = find_prompt(
        lens="correctness",
        focus="logic defects",
        worktree=str(orphan_repo),
        base_branch="main",
        head_sha="a" * 40,
        pr_number=7,
        task_text="t",
        inlined_context=inlined,
    )

    assert "no change detected" not in prompt, (
        "the lens is told 'the only code you can see is the change pasted below' "
        "while the pasted change is a placeholder string containing no code"
    )
    assert "def div" in prompt, "the actual change under review was never inlined"


def test_find_on_unresolvable_range_does_not_produce_a_clean_review(
    tmp_path: Path, orphan_repo: Path
) -> None:
    """End to end: the gate must not record a clean review of a review it never did.

    A reviewer that saw nothing can only return an empty findings list. The gate
    has no way to distinguish that from a genuinely clean PR, so the run ends up
    an approval over a change nobody looked at. The correct behaviour is to fail
    closed (``GateInfraError``), exactly as a dead agent does.
    """
    remote = _RecordingBackend()
    pipe = _pipeline(tmp_path, orphan_repo, remote)
    ref = PullRequestRef(number=7, head_ref="feat", head_sha="a" * 40, state="OPEN")

    try:
        findings = pipe.find(orphan_repo, ref)
    except GateInfraError:
        # Fail-closed is the correct outcome: the gate has no evidence either way.
        return

    assert findings == [], (
        "expected the gate to fail closed on an unresolvable diff range; instead "
        "it completed a review that saw no code and returned no findings"
    )
    # Reaching here means the gate approved. The prompt proves it saw nothing.
    sent = remote.prompts[0]
    assert "no change detected" not in sent, (
        "the gate completed a clean review whose only input was the "
        "'(no change detected)' placeholder"
    )
    assert "def div" in sent, "the real change was never inlined into the prompt"
