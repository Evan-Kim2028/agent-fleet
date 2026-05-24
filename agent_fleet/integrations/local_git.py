"""Local git operations for in-repo or worktree-based runs."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.repo import RepoConfig

logger = logging.getLogger(__name__)


def _resolve_base_ref(repo_root: Path, base_branch: str) -> str:
    for candidate in (base_branch, "main", "master", "HEAD"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate
    return "HEAD"


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

    # Default cap on any git invocation; raised for commit which may run
    # user-defined pre-commit hooks (e.g. gitleaks-via-docker) that can hang.
    _DEFAULT_GIT_TIMEOUT_S = 60
    _COMMIT_GIT_TIMEOUT_S = 600

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        effective_timeout = timeout if timeout is not None else self._DEFAULT_GIT_TIMEOUT_S
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=False,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            logger.error(
                "git %s timed out after %ss in %s", " ".join(args), effective_timeout, cwd
            )
            raise RuntimeError(
                f"git {' '.join(args)} timed out after {effective_timeout}s "
                f"(likely a hung pre-commit hook). stdout={stdout!r} stderr={stderr!r}"
            ) from exc

    def find_resume_branch(
        self,
        task_id: int,
        persona: str,
        branch_prefix: str,
    ) -> tuple[str, str] | None:
        """Return (branch_name, run_id) for an in-progress fleet branch with local changes."""
        prefix = f"{branch_prefix}/{persona}/{task_id}-"
        result = self._run(["branch", "--list", f"{prefix}*"], cwd=self.repo_root)
        candidates = [
            line.strip().lstrip("* ")
            for line in result.stdout.splitlines()
            if line.strip() and line.strip().lstrip("* ").startswith(prefix)
        ]
        for branch_name in reversed(candidates):
            run_id = branch_name.rsplit("-", 1)[-1]
            worktree = self.attach_worktree(branch_name, run_id, create=False)
            if worktree is not None and self.has_workspace_changes(worktree):
                return branch_name, run_id
        return None

    def attach_worktree(
        self,
        branch_name: str,
        run_id: str,
        *,
        create: bool = True,
    ) -> Path | None:
        """Resolve or create a worktree for an existing fleet branch."""
        if not self.use_worktree:
            return self.repo_root

        from agent_fleet.pr_loop.worktree import resolve_worktree_path

        target = resolve_worktree_path(
            branch_name,
            repo_root=self.repo_root,
            worktree_base=self.worktree_base,
        )
        if target.exists() and ((target / ".git").exists() or (target / ".git").is_file()):
            self._active_worktrees.append(target)
            return target
        if not create:
            return None

        target.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(["worktree", "add", str(target), branch_name], cwd=self.repo_root)
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")
        self._active_worktrees.append(target)
        return target

    def has_workspace_changes(self, worktree: Path) -> bool:
        status = self._run(["status", "--porcelain"], cwd=worktree)
        if status.stdout.strip():
            return True
        ahead = self._run(["rev-list", "--count", "HEAD", "--not", "--remotes"], cwd=worktree)
        try:
            return int(ahead.stdout.strip() or "0") > 0
        except ValueError:
            return False

    def setup_workspace(
        self,
        repo_root: Path,
        run_id: str,
        base_branch: str,
        *,
        branch_name: str | None = None,
    ) -> Path:
        if not self.use_worktree:
            return repo_root.resolve()
        self.worktree_base.mkdir(parents=True, exist_ok=True)
        target = self.worktree_base / run_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        start_ref = _resolve_base_ref(repo_root, base_branch)
        args = ["worktree", "add"]
        if branch_name:
            args.extend(["-b", branch_name, str(target), start_ref])
        else:
            args.extend([str(target), start_ref])
        result = self._run(args, cwd=repo_root)
        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")
        self._active_worktrees.append(target)
        return target

    def teardown_workspace(self, worktree: Path, *, forensic: bool = False) -> None:
        if forensic:
            return
        if not self.use_worktree or worktree.resolve() == self.repo_root.resolve():
            return
        self._run(["worktree", "remove", "--force", str(worktree)], cwd=self.repo_root)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)

    def create_branch(self, worktree: Path, branch_name: str) -> None:
        result = self._run(["checkout", "-b", branch_name], cwd=worktree)
        if result.returncode != 0:
            raise RuntimeError(f"git checkout -b failed: {result.stderr.strip()}")

    def commit_changes(
        self,
        worktree: Path,
        message: str,
        *,
        max_hook_retries: int = 2,
    ) -> str | None:
        status = self._run(["status", "--porcelain"], cwd=worktree)
        if not status.stdout.strip():
            return None
        last_stderr = ""
        last_stdout = ""
        for _ in range(max_hook_retries + 1):
            self._run(["add", "-A"], cwd=worktree)
            result = self._run(
                ["commit", "-m", message],
                cwd=worktree,
                timeout=self._COMMIT_GIT_TIMEOUT_S,
            )
            if result.returncode == 0:
                sha = self._run(["rev-parse", "HEAD"], cwd=worktree)
                return sha.stdout.strip() or None
            last_stderr = result.stderr.strip()
            last_stdout = result.stdout.strip()
            # Pre-commit hooks (ruff format, end-of-file-fixer, etc.) often
            # exit non-zero after auto-fixing files. Retry once those fixes
            # show up as unstaged modifications; otherwise it's a real failure.
            post_status = self._run(["status", "--porcelain"], cwd=worktree)
            if not post_status.stdout.strip():
                break
        combined = "\n".join(part for part in (last_stdout, last_stderr) if part)
        raise RuntimeError(f"git commit failed: {combined}")

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
