"""Carrying an approval across a patch-identical rebase.

When a PR has to be rebased onto a moved main, the change is often
*identical* — same diff, new parent commit. Paying for a full
find→verify→judge run to rediscover that is pure waste, and it is the
common case, because the thing that most often forces a rebase is main
moving.

The carry-over is only sound if the change really is the same change. So
it compares ``git patch-id`` of the two diffs — the same identity the
reference bash gate's rebase fast-path used — excluding ``test_gate_*``
files, since those are gate evidence written into the PR's repo and a
rename or an add/add on them is exactly what forced the rebase in the
first place. The result is only trusted when the old approval line
exists *and* the tests are green on the new head merged with the base.

Everything else says a full gate is required. An approval carried onto a
changed patch is worse than no approval at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime
from typing import Any

import pytest

from agent_fleet.contracts.gate import GateOutcome
from agent_fleet.gate import gitops
from agent_fleet.gate.config import GateConfig
from agent_fleet.gate.pipeline import GatePipeline, TestRun
from agent_fleet.model_policy import ModelPolicy

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "recheck-fixture"
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


class _Repo:
    """A main branch, a PR branch, and a way to move main and rebase onto it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main", str(root))
        _git(root, "config", "user.email", "gate@test.local")
        _git(root, "config", "user.name", "Gate Test")
        (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        _git(root, "checkout", "-q", "-b", "fb/lane")
        self.commit("pr change", "agent.py", "VALUE = 2\n")

    def commit(self, message: str, name: str, content: str) -> str:
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(content, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)
        return _git(self.root, "rev-parse", "HEAD")

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def move_main(self, content: str) -> None:
        """Advance main on a side branch, then return to the PR branch."""
        current = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        _git(self.root, "checkout", "-q", "main")
        self.commit("main moves on", "other.py", content)
        _git(self.root, "checkout", "-q", current)

    def rebase(self) -> str:
        _git(self.root, "rebase", "main")
        return self.head()


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    return _Repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# patch-id: the identity check the carry-over rests on
# ---------------------------------------------------------------------------


def test_patch_id_is_stable_across_a_rebase(repo: _Repo) -> None:
    old = repo.head()
    repo.move_main("UNRELATED = 1\n")
    new = repo.rebase()
    assert old != new, "the rebase must actually have changed the head"
    assert gitops.patch_id(repo.root, old, "main") == gitops.patch_id(repo.root, new, "main")


def test_patch_id_differs_when_the_change_differs(repo: _Repo) -> None:
    old = repo.head()
    repo.commit("a different change", "agent.py", "VALUE = 3\n")
    new = repo.head()
    assert gitops.patch_id(repo.root, old, "main") != gitops.patch_id(repo.root, new, "main")


def test_patch_id_ignores_gate_test_files(repo: _Repo) -> None:
    """The whole point of the exclusion: gate evidence is written into the
    PR's repo, and it is what usually collides and forces the rebase."""
    old = repo.head()
    repo.commit(
        "gate test lands",
        "tests/test_gate_fb_lane_c_1.py",
        "def test_x():\n    assert True\n",
    )
    new = repo.head()
    assert gitops.patch_id(repo.root, old, "main") == gitops.patch_id(repo.root, new, "main")


def test_patch_id_is_empty_for_an_unknown_sha(repo: _Repo) -> None:
    assert gitops.patch_id(repo.root, "0" * 40, "main") == ""


# ---------------------------------------------------------------------------
# The approval line the carry-over is anchored to
# ---------------------------------------------------------------------------


def test_an_approval_line_is_found_for_that_sha(tmp_path: Path) -> None:
    status = tmp_path / "lane.status"
    status.write_text("10:00:00 PREMERGE-APPROVED abc123def\n", encoding="utf-8")
    assert gitops.has_approval_line(status, "abc123def")
    assert gitops.has_approval_line(status, "abc123def4567890")


def test_an_approval_for_a_different_sha_does_not_count(tmp_path: Path) -> None:
    status = tmp_path / "lane.status"
    status.write_text("10:00:00 PREMERGE-APPROVED aaaaaaa\n", encoding="utf-8")
    assert not gitops.has_approval_line(status, "abc123def")


def test_a_missing_status_file_is_not_an_approval(tmp_path: Path) -> None:
    assert not gitops.has_approval_line(tmp_path / "nope.status", "abc123def")


def test_an_escalation_line_is_not_an_approval(tmp_path: Path) -> None:
    status = tmp_path / "lane.status"
    status.write_text("10:00:00 NEEDS-ESCALATION stalled after 1 round(s)\n", encoding="utf-8")
    assert not gitops.has_approval_line(status, "abc123def")


# ---------------------------------------------------------------------------
# The carry-over decision, end to end
# ---------------------------------------------------------------------------


def _run_recheck(
    repo: _Repo,
    tmp_path: Path,
    *,
    approved_sha: str,
    head_sha: str,
    status_file: Path,
    tests: TestRun | None = None,
) -> Any:  # noqa: ANN401
    result = TestRun(failing=[], ran=1) if tests is None else tests
    config = GateConfig(backend="cmd", model="m", judge_backend="cmd", enable_judge=False)
    pipe = GatePipeline(
        repo=repo.root,
        pr_number=7,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=object(),  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        status_file=status_file,
        use_systemd=False,
        lane_slug="fb_lane",
    )
    pipe.evidence.gate_tests = []
    return pipe.recheck_carry_over(
        approved_sha=approved_sha,
        head_sha=head_sha,
        status_file=status_file,
        test_run=result,
        test_files=[],
    )


def _approved(repo: _Repo, tmp_path: Path) -> tuple[str, Path]:
    """The PR approved at *old*, status file holding the approval line."""
    old = repo.head()
    status = tmp_path / "lane.status"
    status.write_text(f"10:00:00 PREMERGE-APPROVED {old}\n", encoding="utf-8")
    return old, status


def test_recheck_approves_a_patch_identical_rebase(repo: _Repo, tmp_path: Path) -> None:
    old, status = _approved(repo, tmp_path)
    repo.move_main("UNRELATED = 1\n")
    new = repo.rebase()
    result = _run_recheck(repo, tmp_path, approved_sha=old, head_sha=new, status_file=status)
    assert result.outcome is GateOutcome.APPROVED
    assert result.sha == new
    assert result.approved is True
    assert any("carried over" in r for r in result.reasons)


def test_the_carry_over_status_line_names_the_new_head(repo: _Repo, tmp_path: Path) -> None:
    """The automerge reads the status line, not the JSON, so it has to be the
    new sha — carrying the old one would approve a head nobody tested."""
    from agent_fleet.fleet_ops.gate import _APPROVAL_LINE_RE

    old, status = _approved(repo, tmp_path)
    repo.move_main("UNRELATED = 1\n")
    new = repo.rebase()
    result = _run_recheck(repo, tmp_path, approved_sha=old, head_sha=new, status_file=status)
    assert _APPROVAL_LINE_RE.match(result.status_line.split(" ", 1)[1].strip())
    assert new[:9] in result.status_line


def test_recheck_refuses_when_the_patch_changed(repo: _Repo, tmp_path: Path) -> None:
    old, status = _approved(repo, tmp_path)
    new = repo.commit("a different change", "agent.py", "VALUE = 3\n")
    result = _run_recheck(repo, tmp_path, approved_sha=old, head_sha=new, status_file=status)
    assert result.outcome is GateOutcome.NEEDS_ESCALATION
    assert result.approved is False
    assert any("full gate" in r for r in result.reasons)


def test_recheck_refuses_when_any_test_fails(repo: _Repo, tmp_path: Path) -> None:
    old, status = _approved(repo, tmp_path)
    repo.move_main("UNRELATED = 1\n")
    new = repo.rebase()
    result = _run_recheck(
        repo,
        tmp_path,
        approved_sha=old,
        head_sha=new,
        status_file=status,
        tests=TestRun(failing=["tests/test_pr.py::test_x"], ran=1, tests_failed=True),
    )
    assert result.outcome is GateOutcome.NEEDS_ESCALATION
    assert not result.approved


def test_recheck_refuses_when_the_tests_could_not_run(repo: _Repo, tmp_path: Path) -> None:
    """ "We could not run the tests" is never evidence to carry an approval."""
    old, status = _approved(repo, tmp_path)
    repo.move_main("UNRELATED = 1\n")
    new = repo.rebase()
    result = _run_recheck(
        repo,
        tmp_path,
        approved_sha=old,
        head_sha=new,
        status_file=status,
        tests=TestRun(infra_error="collection error", ran=1),
    )
    assert result.outcome is GateOutcome.NEEDS_ESCALATION
    assert not result.approved


def test_recheck_refuses_without_the_old_approval_line(repo: _Repo, tmp_path: Path) -> None:
    """Patch-identity alone is not an approval; there has to have been one."""
    old = repo.head()
    status = tmp_path / "lane.status"
    status.write_text("10:00:00 NEEDS-ESCALATION stalled\n", encoding="utf-8")
    repo.move_main("UNRELATED = 1\n")
    new = repo.rebase()
    result = _run_recheck(repo, tmp_path, approved_sha=old, head_sha=new, status_file=status)
    assert result.outcome is GateOutcome.NEEDS_ESCALATION
    assert not result.approved
    assert any("full gate" in r for r in result.reasons)


def test_recheck_refuses_when_the_head_is_unknown(repo: _Repo, tmp_path: Path) -> None:
    old, status = _approved(repo, tmp_path)
    result = _run_recheck(repo, tmp_path, approved_sha=old, head_sha="0" * 40, status_file=status)
    assert result.outcome is GateOutcome.NEEDS_ESCALATION
    assert not result.approved


def test_recheck_refuses_without_an_approved_sha(repo: _Repo, tmp_path: Path) -> None:
    status = tmp_path / "lane.status"
    status.write_text("10:00:00 PREMERGE-APPROVED aaaaaaa\n", encoding="utf-8")
    result = _run_recheck(repo, tmp_path, approved_sha="", head_sha=repo.head(), status_file=status)
    assert result.outcome is GateOutcome.NEEDS_ESCALATION
    assert not result.approved


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _patch_recheck(monkeypatch: pytest.MonkeyPatch, result: Any) -> dict[str, Any]:  # noqa: ANN401
    seen: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> Any:  # noqa: ANN401
        seen.update(kwargs)
        return result

    monkeypatch.setattr("agent_fleet.gate.pipeline.run_gate_recheck", _fake)
    return seen


def test_recheck_cli_exits_zero_and_prints_the_status_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_fleet.cli import main
    from agent_fleet.gate.pipeline import GateResult

    result = GateResult(outcome=GateOutcome.APPROVED, sha="abc123def4", run_id="r")
    result.status_line = "10:00:00 PREMERGE-APPROVED abc123def"
    result.reasons = ["approval carried over from aaaaaaa"]
    _patch_recheck(monkeypatch, result)
    rc = main(
        [
            "gate",
            "recheck",
            "--pr",
            "7",
            "--repo-path",
            str(tmp_path),
            "--approved-sha",
            "aaaaaaaaa",
            "--head",
            "abc123def4",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "PREMERGE-APPROVED abc123def" in captured.err
    assert json.loads(captured.out)["outcome"] == "APPROVED"


def test_recheck_cli_exits_nonzero_when_a_full_gate_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_fleet.cli import main
    from agent_fleet.gate.pipeline import GateResult

    result = GateResult(outcome=GateOutcome.NEEDS_ESCALATION, sha="", run_id="r")
    result.status_line = "10:00:00 NEEDS-ESCALATION change differs from the approved patch"
    result.reasons = ["full gate required: change differs from the approved patch"]
    _patch_recheck(monkeypatch, result)
    rc = main(
        [
            "gate",
            "recheck",
            "--pr",
            "7",
            "--repo-path",
            str(tmp_path),
            "--approved-sha",
            "aaaaaaaaa",
            "--head",
            "abc123def4",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "NEEDS-ESCALATION" in captured.err
    assert json.loads(captured.out)["outcome"] == "NEEDS_ESCALATION"


def test_recheck_cli_requires_an_approved_sha(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_fleet.cli import main

    assert (
        main(["gate", "recheck", "--pr", "7", "--repo-path", str(tmp_path), "--head", "abcd"]) == 2
    )
    assert "--approved-sha" in capsys.readouterr().err
