"""The JSON report's ``command`` must record what the trainer actually ran.

Claim (contract-2): ``run_train`` writes ``payload["command"] = command or
test_command_for(())`` — the *empty-set* default, a bare ``pytest`` — while
``GitTrainer._argv`` really runs ``pytest <selected test files>`` over the
batch's changed modules.  The audit record therefore understates what was
tested on the default path, and a reader cannot tie a REGRESSION verdict's
failing test ids to the command that produced them.

This drives the real ``run_train`` (with the real ``GitTrainer`` and a real
fold over a temp git repo) and reads the report it writes, so the assertion is
about the file an operator or downstream automation actually reads.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_fleet.merge_plan.train import (
    REGRESSION,
    TrainPR,
    run_train,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


@pytest.fixture
def repo_with_sibling_test(tmp_path: Path) -> tuple[Path, str]:
    """A checkout whose PR changes ``foo/bar.py``, which has ``foo/test_bar.py``."""
    upstream = tmp_path / "upstream"
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    (upstream / "foo").mkdir(parents=True)
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "T")
    (upstream / "foo" / "bar.py").write_text("VALUE = 0\n", encoding="utf-8")
    # The sibling test that select_test_files will pick up for foo/bar.py.
    (upstream / "foo" / "test_bar.py").write_text("def test_bar():\n    pass\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "base")
    _git(upstream, "clone", "-q", "--bare", ".", str(origin))
    _git(upstream, "remote", "add", "origin", str(origin))
    _git(upstream, "push", "-q", "origin", "main")
    _git(upstream, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")

    _git(upstream, "checkout", "-q", "-b", "feat/one", "main")
    (upstream / "foo" / "bar.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    head = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "push", "-q", "origin", "feat/one")
    return clone, head


def test_the_report_names_the_command_the_trainer_actually_ran(
    repo_with_sibling_test: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report's ``command`` must be the per-run argv, not the empty default.

    The claim: on the default path (no ``--test-command``), the report records a
    bare ``pytest`` even though the trainer ran ``pytest foo/test_bar.py``.
    """
    clone, head = repo_with_sibling_test
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))

    ran: list[list[str]] = []

    def fake_run(
        argv: list[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if argv[:1] == ["pytest"]:
            ran.append(list(argv))
            return subprocess.CompletedProcess(
                argv, 1, stdout="FAILED foo/test_bar.py::test_bar - boom\n", stderr=""
            )
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    monkeypatch.setattr("agent_fleet.merge_plan.train._run", fake_run)

    pr = TrainPR(number=1, head_sha=head, base_ref="main", files=("foo/bar.py",))
    report = tmp_path / "report.json"
    result = run_train(
        repo="demo",
        repo_path=clone,
        prs=[pr],
        report_path=report,
    )

    # Precondition: the trainer really did run a narrowed, per-batch command.
    assert ran, "the trainer never ran a test command"
    assert ran[0] == ["pytest", "foo/test_bar.py"], (
        f"expected the trainer to select the sibling test, ran {ran[0]}"
    )

    # The bug: the report does not name that command.
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["command"] != "pytest", (
        "report records a bare 'pytest' but the trainer ran "
        f"{ran[0]!r}; the audit record understates what was tested"
    )
    # More precisely: it should match the argv the trainer actually built.
    assert payload["command"] == " ".join(ran[0]), (
        f"report command {payload['command']!r} != the command actually run {ran[0]!r}"
    )

    # And the failing test ids the verdict carries must be attributable to it.
    assert [v.pr for v in result.by_status(REGRESSION)] == [1]
    assert result.by_status(REGRESSION)[0].failing_tests == ("foo/test_bar.py::test_bar",)


def test_the_report_command_diverges_from_the_trainer_argv_directly(
    repo_with_sibling_test: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate the divergence: report command != command the trainer built.

    This does not depend on the run going red; it compares the two values the
    claim says diverge — ``run_train``'s ``payload["command"]`` and the argv
    ``GitTrainer._argv`` produces for the same batch.
    """
    clone, head = repo_with_sibling_test
    monkeypatch.setenv("AGENT_FLEET_HOME", str(tmp_path / "home"))
    report = tmp_path / "report2.json"

    # Green run, so the report is written and a verdict is LANDED.
    def fake_run(
        argv: list[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if argv[:1] == ["pytest"]:
            return subprocess.CompletedProcess(argv, 0, stdout="1 passed", stderr="")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    monkeypatch.setattr("agent_fleet.merge_plan.train._run", fake_run)

    pr = TrainPR(number=1, head_sha=head, base_ref="main", files=("foo/bar.py",))
    run_train(repo="demo", repo_path=clone, prs=[pr], report_path=report)

    payload = json.loads(report.read_text(encoding="utf-8"))

    # The trainer, for this same batch and tree, builds a narrowed argv.
    from agent_fleet.merge_plan.train import select_test_files, test_command_for

    selected = select_test_files([pr], tree=clone)
    actual = test_command_for(selected)
    assert selected == ["foo/test_bar.py"]
    assert actual == "pytest foo/test_bar.py"
    assert payload["command"] != actual, (
        f"report command {payload['command']!r} != the narrowed command {actual!r}"
    )
