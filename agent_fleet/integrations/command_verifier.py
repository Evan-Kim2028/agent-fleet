"""Command-based verifier driven by repo .agent-fleet.yaml."""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING

from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity
from agent_fleet.observability.fleet_logger import emit_fleet_event
from agent_fleet.verify_core import get_changed_files

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.repo import RepoConfig


def _format_failure(headline: str, proc: subprocess.CompletedProcess[str]) -> str:
    detail = (proc.stderr or proc.stdout or "")[-2000:].rstrip()
    if not detail:
        return f"{headline}\nexit={proc.returncode}"
    return f"{headline}\nexit={proc.returncode}\n{detail}"


class CommandVerifier:
    """Run configured shell commands as verification gates."""

    def __init__(self, repo: RepoConfig) -> None:
        self.repo = repo

    def check(
        self,
        worktree: Path,
        *,
        persona: str,
        changed_files: list[Path],
        task_id: int,
    ) -> VerifyResult:
        del changed_files
        rel_changed = get_changed_files(worktree)
        checks: list[dict] = []
        verify_env = {
            **os.environ,
            "ISSUE_NUMBER": str(task_id),
            "FLEET_PERSONA": persona,
        }
        bootstrap_commands = list(self.repo.worktree_bootstrap_commands)
        bootstrap_t0 = time.monotonic()
        bootstrap_exit_code = 0
        bootstrap_fatal_proc: subprocess.CompletedProcess[str] | None = None
        bootstrap_fatal_cmd: str | None = None
        for cmd in bootstrap_commands:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
                env=verify_env,
            )
            checks.append(
                {
                    "name": f"bootstrap: {cmd}",
                    "passed": proc.returncode == 0,
                    "stdout_tail": proc.stdout[-2000:],
                    "stderr_tail": proc.stderr[-2000:],
                    "exit_code": proc.returncode,
                }
            )
            if proc.returncode != 0:
                bootstrap_exit_code = proc.returncode
                bootstrap_fatal_proc = proc
                bootstrap_fatal_cmd = cmd
                break
        if bootstrap_commands:
            emit_fleet_event(
                "worktree.bootstrap",
                commands=bootstrap_commands,
                duration_s=round(time.monotonic() - bootstrap_t0, 3),
                exit_code=bootstrap_exit_code,
            )
        if bootstrap_fatal_proc is not None and bootstrap_fatal_cmd is not None:
            # Bootstrap prepares the worktree. It is deterministic on
            # rerun and not fixable by editing the code under task
            # (lockfile drift, missing tools, network). Classify FATAL
            # so the runner bails immediately instead of burning fix
            # iterations on an environmental problem.
            return VerifyResult(
                severity=VerifySeverity.FATAL,
                checks=checks,
                violating_paths=[],
                files_changed=rel_changed,
                message=_format_failure(
                    f"Worktree bootstrap failed: {bootstrap_fatal_cmd}",
                    bootstrap_fatal_proc,
                ),
            )

        verify_commands_ran = bool(self.repo.verify_commands)
        for cmd in self.repo.verify_commands:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
                env=verify_env,
            )
            checks.append(
                {
                    "name": cmd,
                    "passed": proc.returncode == 0,
                    "stdout_tail": proc.stdout[-2000:],
                    "stderr_tail": proc.stderr[-2000:],
                    "exit_code": proc.returncode,
                }
            )
            if proc.returncode != 0:
                # Auto-apply `ruff check --fix` once before counting this as a
                # failure. This prevents the fix loop from burning attempts on
                # import-sorting (I001) and other auto-fixable lint issues.
                # Scope is intentionally narrow — only `ruff check` triggers it.
                if "ruff check" in cmd and "--fix" not in cmd:
                    fix_cmd = cmd + " --fix"
                    subprocess.run(
                        fix_cmd,
                        shell=True,
                        cwd=str(worktree),
                        capture_output=True,
                        text=True,
                        check=False,
                        env=verify_env,
                    )
                    rerun_proc = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=str(worktree),
                        capture_output=True,
                        text=True,
                        check=False,
                        env=verify_env,
                    )
                    # Count files changed by ruff --fix via git diff --name-only
                    _files_changed_count = 0
                    try:
                        _diff = subprocess.run(
                            ["git", "diff", "--name-only"],
                            cwd=str(worktree),
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        _files_changed_count = len(
                            [ln for ln in _diff.stdout.splitlines() if ln.strip()]
                        )
                    except Exception:
                        pass
                    emit_fleet_event(
                        "verify.autofix.applied",
                        data={
                            "command": cmd,
                            "before_exit": proc.returncode,
                            "after_exit": rerun_proc.returncode,
                            "files_changed_count": _files_changed_count,
                        },
                    )
                    checks[-1] = {
                        "name": cmd,
                        "passed": rerun_proc.returncode == 0,
                        "stdout_tail": rerun_proc.stdout[-2000:],
                        "stderr_tail": rerun_proc.stderr[-2000:],
                        "exit_code": rerun_proc.returncode,
                        "autofix_applied": True,
                    }
                    if rerun_proc.returncode == 0:
                        continue
                    proc = rerun_proc
                return VerifyResult(
                    severity=VerifySeverity.RETRY,
                    checks=checks,
                    violating_paths=[],
                    files_changed=rel_changed,
                    message=_format_failure(f"Verification failed: {cmd}", proc),
                )

        if not verify_commands_ran:
            blocked = [
                p
                for p in rel_changed
                if any(p.startswith(prefix) for prefix in self.repo.critical_path_prefixes)
            ]
            if blocked:
                return VerifyResult(
                    severity=VerifySeverity.FATAL,
                    checks=checks,
                    violating_paths=blocked,
                    files_changed=rel_changed,
                    message=f"Modified protected paths: {', '.join(blocked)}",
                )

        if not self.repo.verify_commands and not rel_changed:
            return VerifyResult(
                severity=VerifySeverity.OK,
                checks=checks,
                violating_paths=[],
                files_changed=[],
                message="No changes detected",
            )

        return VerifyResult(
            severity=VerifySeverity.OK,
            checks=checks,
            violating_paths=[],
            files_changed=rel_changed,
            message="All verification checks passed",
        )
