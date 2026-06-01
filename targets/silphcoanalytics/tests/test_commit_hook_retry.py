"""Shared pre-commit-hook-tolerant commit helper.

Regression for the #1180 crash: the fleet repo has an active
`.git/hooks/pre-commit` (ruff/prettier-style). When a formatting hook
reformats files it aborts the commit with a non-zero exit and leaves the
edits in the working tree. The three commit call sites in the dispatch /
phases path used a raw ``subprocess.run(["git","commit",...], check=True)``,
so a reformatting hook crashed the whole run (#1180: frontend persona,
verify passed, commit died, PR lost). ``commit_with_hook_retry`` re-stages
and retries once — the same logic the fleet runner already had — and never
raises: it returns whether a commit was created.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import agents.github as ghmod
from agents.github import commit_with_hook_retry


class _FakeRun:
    """Callable subprocess.run fake.

    *commit_codes* is the list of return codes successive `git commit`
    invocations should yield. `git status --porcelain` reports a dirty tree
    while *tree_dirty_after_fail* is True.
    """

    def __init__(self, commit_codes, tree_dirty_after_fail=True):
        self.state = {"commits": 0}
        self.recorded: list[list[str]] = []
        self.commit_codes = commit_codes
        self.tree_dirty_after_fail = tree_dirty_after_fail

    def __call__(self, cmd, *args, **kwargs):
        self.recorded.append(list(cmd) if isinstance(cmd, list) else cmd)
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
        if isinstance(cmd, list) and cmd[:2] == ["git", "commit"]:
            i = self.state["commits"]
            self.state["commits"] += 1
            mock.returncode = self.commit_codes[i] if i < len(self.commit_codes) else 0
            if mock.returncode != 0:
                mock.stderr = "ruff format...Failed\n- files were modified by this hook\n"
        elif "status" in cmd_str and "--porcelain" in cmd_str:
            mock.stdout = "M agents/x.py\n" if self.tree_dirty_after_fail else ""
        return mock


def test_returns_true_on_clean_commit(monkeypatch):
    fake = _FakeRun([0])
    monkeypatch.setattr(ghmod.subprocess, "run", fake)
    assert commit_with_hook_retry("/tmp/wt", "msg") is True
    assert fake.state["commits"] == 1


def test_retries_on_reformat_then_succeeds(monkeypatch):
    fake = _FakeRun([1, 0], tree_dirty_after_fail=True)
    monkeypatch.setattr(ghmod.subprocess, "run", fake)
    assert commit_with_hook_retry("/tmp/wt", "msg") is True
    assert fake.state["commits"] == 2
    add_calls = [c for c in fake.recorded if c[:3] == ["git", "add", "-A"]]
    assert len(add_calls) >= 1


def test_no_retry_when_tree_clean(monkeypatch):
    fake = _FakeRun([1], tree_dirty_after_fail=False)
    monkeypatch.setattr(ghmod.subprocess, "run", fake)
    assert commit_with_hook_retry("/tmp/wt", "msg") is False
    assert fake.state["commits"] == 1  # no retry


def test_retry_also_fails_returns_false(monkeypatch):
    fake = _FakeRun([1, 1], tree_dirty_after_fail=True)
    monkeypatch.setattr(ghmod.subprocess, "run", fake)
    assert commit_with_hook_retry("/tmp/wt", "msg") is False
    assert fake.state["commits"] == 2


def test_timeout_returns_false(monkeypatch):
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git commit", timeout=30)

    monkeypatch.setattr(ghmod.subprocess, "run", boom)
    assert commit_with_hook_retry("/tmp/wt", "msg") is False
