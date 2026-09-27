"""A hang inside the fold is reported, not raised past the command.

``_run`` gives every git call it makes a 600s timeout, and the only one that
branches on the result being a timeout is the *test* command.  The fold's own
calls — the base fetch, ``worktree add``, the per-PR merge — have no such
branch, so a git that hangs there (a stuck NFS mount, a credential prompt with
no tty) raises ``subprocess.TimeoutExpired`` straight through.

``cmd_merge_train`` catches only ``(OSError, RuntimeError)`` and
``TimeoutExpired`` is neither, so the exception leaves the command as a raw
Python traceback instead of the ``error: ...`` sentence every other fold
failure produces.  Worse, the candidate directory is created before the
``worktree add`` that timed out, and ``__exit__`` only cleans up a worktree it
recorded as its own — so the run leaves the directory *and* its half-finished
registration behind in the repository's own ``.git``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from agent_fleet.merge_plan.collect import GitHubClient

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _registered_worktrees(repo: Path) -> list[str]:
    """The worktree paths git itself holds registered for *repo*."""
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return [line[len("worktree ") :] for line in out.splitlines() if line.startswith("worktree ")]


def _repo_with_one_pr(root: Path) -> tuple[Path, str]:
    """A clone of a local ``origin`` holding a base and one pushed PR head.

    The clone is taken before the PR is cut, so it holds the base and none of
    the PR heads: the state an operator's checkout is in when the train runs,
    and the state the fold's own fetch has to cope with.
    """
    upstream = root / "upstream"
    origin = root / "origin.git"
    clone = root / "clone"
    upstream.mkdir(parents=True)
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "T")
    (upstream / "a.txt").write_text("a0\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "base")
    _git(upstream, "clone", "-q", "--bare", ".", str(origin))
    _git(upstream, "remote", "add", "origin", str(origin))
    _git(upstream, "push", "-q", "origin", "main")
    _git(upstream, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    _git(upstream, "checkout", "-q", "-b", "feat/one", "main")
    (upstream / "a.txt").write_text("a1\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "one")
    head = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "push", "-q", "origin", "feat/one")
    return clone, head


class _StubDetailClient:
    """A ``GitHubClient`` whose every PR is open at the head we really pushed."""

    def __init__(self, head: str) -> None:
        self._head = head

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        del pr_number
        return {
            "state": "OPEN",
            "headRefOid": self._head,
            "headRefName": "fb/one",
            "baseRefName": "main",
            "files": [{"path": "a.py"}],
        }


def _for_repo_returning(
    client: _StubDetailClient,
) -> Callable[[GitHubClient, Path], _StubDetailClient]:
    """A ``GitHubClient.for_repo`` replacement handing back *client*."""

    def _factory(self: GitHubClient, repo_path: Path) -> _StubDetailClient:
        del self, repo_path
        return client

    return _factory


def test_a_hanging_git_call_in_the_fold_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A git that hangs in the fold must not escape cmd_merge_train as a traceback.

    Every other fold failure — a base that will not fetch, a branch that does
    not exist — is a ``RuntimeError`` the command turns into an ``error: ...``
    line and an exit code.  A timeout is not a ``RuntimeError``, so today the
    only thing standing between an operator and a traceback is the hang being
    rare.  This drives the real command over a real repository and fails
    ``worktree add`` by timeout, which is the first fold call to run against a
    checkout with a directory already made for it.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    checkout, head = _repo_with_one_pr(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))

    status = tmp_path / "status"
    status.mkdir()
    (status / "gate.md").write_text(
        f"Evan-Kim2028/demo#12\nPREMERGE-APPROVED {head}\n", encoding="utf-8"
    )
    monkeypatch.setattr(GitHubClient, "for_repo", _for_repo_returning(_StubDetailClient(head)))

    def hang_on_worktree_add(
        argv: Sequence[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    monkeypatch.setattr("agent_fleet.merge_plan.train._run", hang_on_worktree_add)

    args = argparse.Namespace(
        repo_path=str(checkout),
        repo="demo",
        config=None,
        base_branch="main",
        operator=None,
        status_dir=str(status),
        test_command="pytest",
        max_batch_size=5,
        report=None,
        dry_run=False,
        json=False,
    )

    try:
        code = merge_cli.cmd_merge_train(args)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"TimeoutExpired escaped cmd_merge_train as {exc!r}; a hanging git call in the "
            f"fold must be reported on stderr with an exit code, like every other fold failure"
        )

    # The command came back, so now it has to have said something usable.
    captured = capsys.readouterr()
    assert code == 2, f"expected the fold failure to be reported as exit 2, got {code!r}"
    assert captured.err.startswith("error: "), (
        f"a fold failure has to be reported as an error sentence, got stderr {captured.err!r}"
    )
    assert "timed out" in captured.err, (
        f"the error has to name the timeout that stopped the fold, got {captured.err!r}"
    )

    # And the run it abandoned must not leave a candidate behind for the next
    # one to collide with: git worktree add refuses to reuse a registered path.
    leftover = [p for p in _registered_worktrees(checkout) if "wt-" in Path(p).name]
    assert not leftover, f"the timed-out run left candidate worktrees registered: {leftover}"


def test_a_hanging_git_call_writes_no_report_and_claims_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hang must not be reported as a train that ran and landed nothing.

    A caller reading the JSON report cannot otherwise tell a train that folded,
    tested and set every PR aside from a train that never got as far as the
    fold — the report for the second does not exist, and the command does not
    say why.
    """
    from agent_fleet.merge_plan import cli as merge_cli

    checkout, head = _repo_with_one_pr(tmp_path / "run")
    home = tmp_path / "home"
    monkeypatch.setenv("AGENT_FLEET_HOME", str(home))

    status = tmp_path / "status"
    status.mkdir()
    (status / "gate.md").write_text(
        f"Evan-Kim2028/demo#12\nPREMERGE-APPROVED {head}\n", encoding="utf-8"
    )
    monkeypatch.setattr(GitHubClient, "for_repo", _for_repo_returning(_StubDetailClient(head)))

    def hang_on_fetch(
        argv: Sequence[str], *, cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "fetch"] and "origin" in argv[2:]:
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    monkeypatch.setattr("agent_fleet.merge_plan.train._run", hang_on_fetch)

    report = tmp_path / "report.json"
    args = argparse.Namespace(
        repo_path=str(checkout),
        repo="demo",
        config=None,
        base_branch="main",
        operator=None,
        status_dir=str(status),
        test_command="pytest",
        max_batch_size=5,
        report=str(report),
        dry_run=False,
        json=True,
    )

    try:
        merge_cli.cmd_merge_train(args)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"TimeoutExpired escaped cmd_merge_train as {exc!r} on the fold's fetch")

    if report.exists():
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload.get("landed") in (None, [], False) or not payload.get("verdicts"), (
            f"a train that never folded must not report verdicts: {payload!r}"
        )
