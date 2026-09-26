"""The recheck's own test asserts a whole file is marker-free after seeding one (contract-1).

``tests/test_gate_gaterobust_prodsafety_1.py`` builds its fixture by writing

    10:00:00 PREMERGE-APPROVED <approved-sha>

into the status file — that line is *condition 2* of the carry-over decision
(``has_approval_line``) — and then closes with

    assert "PREMERGE-APPROVED" not in status.read_text()

over the whole file. The line the fixture just seeded satisfies that assertion
by itself, so the test fails whatever the product does. The recheck under test
behaves correctly: it refuses, and the line it appends is a NEEDS-ESCALATION
with no marker.

This file reproduces the exact assertion, alongside the product-level
assertions that hold, to show which of the two is wrong. The final assertion is
the claimed defect: it is unreachable by construction and leaves a permanently
red test in the merged suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path  # noqa: TC003 - built into concrete paths at runtime

from agent_fleet.contracts.gate import GateOutcome
from agent_fleet.gate.pipeline import run_gate_recheck

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "recheck-fixture"
version = "0.0.0"
"""

MARKER = "PREMERGE-APPROVED"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


def _build_repo(root: Path) -> tuple[Path, str]:
    """A main branch, a PR that changes only source, then a rebase onto a moved main."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "gate@test.local")
    _git(root, "config", "user.name", "Gate Test")
    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _git(root, "checkout", "-q", "-b", "fb/lane")
    (root / "agent.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr change")
    approved = _git(root, "rev-parse", "HEAD")

    _git(root, "checkout", "-q", "main")
    (root / "other.py").write_text("UNRELATED = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "main moves on")
    _git(root, "checkout", "-q", "fb/lane")
    _git(root, "rebase", "main")
    return root, approved


def test_recheck_does_not_approve_when_no_test_ever_ran(tmp_path: Path) -> None:
    """The product refuses correctly; the whole-file assertion cannot ever hold."""
    repo, approved = _build_repo(tmp_path / "repo")
    head = _git(repo, "rev-parse", "HEAD")
    assert head != approved, "the rebase must actually have moved the head"

    # Condition 2 of the decision: the approval the carry-over consumes.
    status = tmp_path / "lane.status"
    status.write_text(f"10:00:00 {MARKER} {approved}\n", encoding="utf-8")

    result = run_gate_recheck(
        repo_path=repo,
        pr_number=7,
        approved_sha=approved,
        head_sha=head,
        status_file=status,
        config_path=str(tmp_path / "no-such-fleet.yaml"),
        use_systemd=False,
    )

    # The product behaviour under test is correct: zero verification is not a pass.
    assert result.outcome is not GateOutcome.APPROVED, (
        f"recheck approved a head with no test run: {result.reasons}"
    )
    assert result.approved is False
    assert MARKER not in result.status_line, (
        f"the emitted line quotes the marker: {result.status_line!r}"
    )

    # The defect: this reads the fixture's own seed line back and fails regardless
    # of the recheck's verdict, because the seed is required for the run to get
    # as far as approving anything at all.
    assert MARKER not in status.read_text(encoding="utf-8")
