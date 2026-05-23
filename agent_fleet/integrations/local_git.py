"""Local git operations for in-repo or worktree-based runs."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from agent_fleet.repo import RepoConfig

logger = logging.getLogger(__name__)


class LocalGitOps:
    """Git operations scoped to a repository."""

    def __init__(
        self,
        repo_root: Path,
        *,
        use_worktree: bool = False,
        worktree_base: Path | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.use_worktree = use_worktree
        self.worktree_base = (worktree_base or Path("/tmp/agent-fleet-worktrees")).resolve()
        self._active_worktrees: list[Path] = []

    def _run(self, args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    def setup_workspace(self, repo_root: Path, run_id: str, base_branch: str) -> Path:
        if not self.use_worktree:
            return repo_root.resolve()
        self.worktree_base.mkdir(parents=True, exist_ok=True)
        target = self.worktree_base / run_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        result = self._run(
            ["worktree", "add", str(target), base_branch],
            cwd=repo_root,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")
        self._active_worktrees.append(target)
        return target

    def teardown_workspace(self, worktree: Path, *, forensic: bool = False) -> None:
        del forensic
        if not self.use_worktree or worktree.resolve() == self.repo_root.resolve():
            return
        self._run(["worktree", "remove", "--force", str(worktree)], cwd=self.repo_root)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)

    def create_branch(self, worktree: Path, branch_name: str) -> None:
        result = self._run(["checkout", "-b", branch_name], cwd=worktree)
        if result.returncode != 0:
            raise RuntimeError(f"git checkout -b failed: {result.stderr.strip()}")

    def commit_changes(self, worktree: Path, message: str) -> str | None:
        status = self._run(["status", "--porcelain"], cwd=worktree)
        if not status.stdout.strip():
            return None
        self._run(["add", "-A"], cwd=worktree)
        result = self._run(["commit", "-m", message], cwd=worktree)
        if result.returncode != 0:
            raise RuntimeError(f"git commit failed: {result.stderr.strip()}")
        sha = self._run(["rev-parse", "HEAD"], cwd=worktree)
        return sha.stdout.strip() or None

    def push_branch(self, worktree: Path, branch_name: str) -> None:
        result = self._run(["push", "-u", "origin", branch_name], cwd=worktree)
        if result.returncode != 0:
            raise RuntimeError(f"git push failed: {result.stderr.strip()}")

    def changed_files(self, worktree: Path) -> list[Path]:
        result = self._run(["status", "--porcelain"], cwd=worktree)
        files: list[Path] = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files.append(worktree / path)
        return files

    def diff_summary(self, worktree: Path) -> str:
        result = self._run(["diff", "HEAD"], cwd=worktree)
        if result.stdout.strip():
            return result.stdout
        staged = self._run(["diff", "--cached"], cwd=worktree)
        return staged.stdout


def git_ops_from_repo(repo: RepoConfig) -> LocalGitOps:
    return LocalGitOps(
        repo.repo_root,
        use_worktree=repo.use_worktree,
        worktree_base=repo.worktree_base,
    )
