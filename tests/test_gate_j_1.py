"""A symlink in the changed-file set must not exfiltrate out-of-worktree files.

``build_review_context`` pastes the post-change content of every changed
non-test file into the lens prompt, because the reviewing backend (OpenRouter)
has no shell and no access to the worktree. That paste is transmitted to a
third-party API, so the set of files it may read has to be exactly the files
the PR changed *inside* the worktree.

The changed-file list comes from ``git diff --name-only``, and the path is then
assembled as ``root / rel`` with no symlink resolution or containment check.
A PR can therefore add a symlink pointing anywhere on the host (an ssh key, a
dotfile, a config with a token) and have its content read and sent off-box.

This test builds exactly that: a repo whose base is on ``main``, a branch that
adds a symlink escaping the worktree, then renders the review context the
OpenRouter-backed ``find``/``judge`` roles would send.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_fleet.gate.inline import build_review_context

SECRET = "GATE-J-1-CANARY-e91c2a7b4f\n"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return done.stdout


def _repo_with_escaping_symlink(tmp_path: Path) -> Path:
    """A branch whose only change is a symlink out of the worktree."""
    secret = tmp_path / "outside" / "id_ed25519"
    secret.parent.mkdir(parents=True)
    secret.write_text(SECRET, encoding="utf-8")

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "-q", "-b", feat_branch := "feat")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    (repo / "src" / "leak.txt").symlink_to(secret)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add a symlink")
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == feat_branch
    return repo


def test_symlink_in_the_change_does_not_leak_out_of_worktree_content(
    tmp_path: Path,
) -> None:
    repo = _repo_with_escaping_symlink(tmp_path)

    # The PR is what the attacker controls: prove the symlink really is in the
    # changed-file set the gate derives from git, so this is not a vacuous test.
    changed = _git(repo, "diff", "--name-only", "main...HEAD")
    assert "src/leak.txt" in changed

    ctx = build_review_context(repo, "main")

    leaked = [f for f in ctx.files if SECRET in f.text]
    assert leaked == [], (
        "build_review_context read through the symlink src/leak.txt and would "
        f"paste out-of-worktree content into the prompt: {[f.path for f in leaked]}"
    )
    # Same check on the rendered block, which is what actually goes on the wire.
    assert SECRET not in ctx.render(), (
        "the rendered review context (sent verbatim to the OpenRouter backend) "
        "contains content from outside the worktree"
    )
