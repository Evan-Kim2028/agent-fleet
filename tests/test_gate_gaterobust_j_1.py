"""The approval-line parser must enforce the automerge's line contract.

``agent_fleet/fleet_ops/gate.py`` reads a lane verdict with an anchored
regex — ``^(?:\\d{2}:\\d{2}:\\d{2}\\s+)?PREMERGE-APPROVED\\s+[0-9a-f]{7,40}\\s*$`` —
so a trailing reason, a leading token, or a non-sha word after the marker
can never be an approval. ``gitops.has_approval_line`` is supposed to be
"the same line contract the automerge reads" (its own docstring), but it
only tokenizes the line and prefix-matches the token after the marker.

Two ways that departs from the contract:

1. a ``NEEDS-ESCALATION`` line whose reason quotes the marker passes, and
2. the prefix is ``sha[:9]``, so a 3-character ``approved_sha`` matches any
   line whose next token begins with those 3 characters.

Both mean a verdict nobody approved can satisfy condition 2 of
``recheck_carry_over`` and let an approval be written for a head that was
never approved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime

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
name = "j1-fixture"
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
# The parser itself
# ---------------------------------------------------------------------------


def test_a_needs_escalation_line_mentioning_the_marker_is_not_an_approval(
    tmp_path: Path,
) -> None:
    """The gate's own escalation text quotes the marker inline.

    ``status_line_for`` puts the first reason on the NEEDS-ESCALATION line,
    and reasons routinely name the marker ("gate already wrote
    PREMERGE-APPROVED <sha>"). The automerge's anchored regex rejects
    that; the carry-over parser must not read it as an approval.
    """
    status = tmp_path / "lane.status"
    status.write_text(
        "10:00:00 NEEDS-ESCALATION full gate required: gate already wrote "
        "PREMERGE-APPROVED 12ab34cd\n",
        encoding="utf-8",
    )
    assert not gitops.has_approval_line(status, "12ab34cd")


def test_trailing_prose_after_the_marker_is_not_an_approval(tmp_path: Path) -> None:
    """A git log line or PR-body echo: the contract allows nothing after the sha."""
    status = tmp_path / "lane.status"
    status.write_text(
        "10:00:00 PREMERGE-APPROVED 12ab34cd (see PR #109 discussion)\n",
        encoding="utf-8",
    )
    assert not gitops.has_approval_line(status, "12ab34cd")


def test_a_three_character_sha_does_not_match_by_prefix(tmp_path: Path) -> None:
    """The status line is written at 9 chars, so a 3-char approved_sha must not
    be satisfied by a line whose sha merely starts with those three characters —
    "123" and "1234abcd" are different commits."""
    status = tmp_path / "lane.status"
    status.write_text("10:00:00 PREMERGE-APPROVED 1234abcd\n", encoding="utf-8")
    assert not gitops.has_approval_line(status, "123")


def test_a_real_approval_line_is_still_accepted(tmp_path: Path) -> None:
    """The contract as written: marker first, a 7-40 hex sha, nothing after."""
    status = tmp_path / "lane.status"
    status.write_text("10:00:00 PREMERGE-APPROVED 12ab34cde\n", encoding="utf-8")
    assert gitops.has_approval_line(status, "12ab34cde")


# ---------------------------------------------------------------------------
# End to end: the carry-over must not approve on a non-approval line
# ---------------------------------------------------------------------------


def test_carry_over_refuses_when_the_status_file_only_escalated(
    repo: _Repo, tmp_path: Path
) -> None:
    """The whole point: patch-identical, tests green, and still no approval
    exists — so the recheck must escalate, not write PREMERGE-APPROVED for a
    head nobody approved."""
    old = repo.head()
    status = tmp_path / "lane.status"
    status.write_text(
        f"10:00:00 NEEDS-ESCALATION full gate required: gate already wrote "
        f"PREMERGE-APPROVED {old[:9]}\n",
        encoding="utf-8",
    )
    repo.move_main("UNRELATED = 1\n")
    new = repo.rebase()

    config = GateConfig(backend="cmd", model="m", judge_backend="cmd", enable_judge=False)
    pipe = GatePipeline(
        repo=repo.root,
        pr_number=7,
        config=config,
        policy=ModelPolicy(backends={}),
        backend=object(),  # type: ignore[arg-type]
        gate_dir=tmp_path / "gate",
        status_file=status,
        use_systemd=False,
        lane_slug="fb_lane",
    )
    pipe.evidence.gate_tests = []
    result = pipe.recheck_carry_over(
        approved_sha=old,
        head_sha=new,
        status_file=status,
        test_run=TestRun(failing=[], ran=1),
        test_files=[],
    )

    assert result.outcome is GateOutcome.NEEDS_ESCALATION, (
        f"carry-over approved on a line that never approved anything: {result.status_line!r}"
    )
    assert result.approved is False
    assert any("full gate" in r for r in result.reasons)
