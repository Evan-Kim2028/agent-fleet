"""A carried-over approval must not be written for a head nothing verified.

``run_gate_recheck`` is fail-closed by contract: the recheck approves only
when a test run *actually happened* and was green. A PR that changes no test
file and has no archived gate tests yields an empty test set, so
``GateTestRunner.run([])`` returns ``TestRun(ran=0, infra_error="")`` — the
same shape as a real green run to every check the current code performs
(``infra_error`` empty, ``failing`` empty). That is "we ran nothing", not "the
tests passed", and treating it as green writes a ``PREMERGE-APPROVED`` line
for the new head that the automerge will act on, on zero verification.
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

    # main moves on, then the PR is rebased onto it: same patch, new head sha
    _git(root, "checkout", "-q", "main")
    (root / "other.py").write_text("UNRELATED = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "main moves on")
    _git(root, "checkout", "-q", "fb/lane")
    _git(root, "rebase", "main")
    return root, approved


def test_recheck_does_not_approve_when_no_test_ever_ran(tmp_path: Path) -> None:
    repo, approved = _build_repo(tmp_path / "repo")
    head = _git(repo, "rev-parse", "HEAD")
    assert head != approved, "the rebase must actually have moved the head"

    status = tmp_path / "lane.status"
    status.write_text(f"10:00:00 PREMERGE-APPROVED {approved}\n", encoding="utf-8")

    result = run_gate_recheck(
        repo_path=repo,
        pr_number=7,
        approved_sha=approved,
        head_sha=head,
        status_file=status,
        config_path=str(tmp_path / "no-such-fleet.yaml"),
        use_systemd=False,
    )

    # The test set is empty here: no changed test files, no archived gate tests.
    # Zero verification must never read as green.
    assert result.outcome is not GateOutcome.APPROVED, (
        f"recheck approved a head with no test run: {result.reasons}"
    )
    assert result.approved is False
    assert "PREMERGE-APPROVED" not in result.status_line
    assert "PREMERGE-APPROVED" not in status.read_text(encoding="utf-8")
