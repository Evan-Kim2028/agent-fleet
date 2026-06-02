"""Command-based verifier driven by repo .agent-fleet.yaml."""

from __future__ import annotations

import os
import re
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


_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def _parse_failed_ids(stdout: str, stderr: str, returncode: int) -> frozenset[str] | None:
    """Parse pytest failing node-ids from a command's output.

    Returns an empty set when the command passed, the set of failing node-ids
    when the pytest summary can be parsed, or ``None`` for an opaque failure
    (non-zero exit with no parseable node-ids, e.g. a lint error or a collection
    crash). Tokens without a ``.py`` segment are dropped so generic ``ERROR``
    log lines are not mistaken for tests.
    """
    if returncode == 0:
        return frozenset()
    ids = frozenset(
        tok for m in _FAILED_RE.finditer(f"{stdout}\n{stderr}") if ".py" in (tok := m.group(1))
    )
    return ids or None


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

        commands = self.repo.verify_commands_for(persona)
        verify_commands_ran = bool(commands)
        for cmd in commands:
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
                preexisting, new_ids = self._preexisting_only(worktree, cmd, verify_env, proc)
                if preexisting:
                    checks[-1]["passed"] = True
                    checks[-1]["attributed_preexisting"] = True
                    emit_fleet_event(
                        "verify.preexisting_skipped",
                        data={"command": cmd, "exit_code": proc.returncode},
                    )
                    continue
                headline = f"Verification failed: {cmd}"
                if new_ids:
                    shown = ", ".join(sorted(new_ids)[:20])
                    headline = (
                        f"Verification failed: {cmd}\n"
                        f"New failures introduced by this change ({len(new_ids)}): {shown}"
                    )
                return VerifyResult(
                    severity=VerifySeverity.RETRY,
                    checks=checks,
                    violating_paths=[],
                    files_changed=rel_changed,
                    message=_format_failure(headline, proc),
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

        if not commands and not rel_changed:
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

    def _preexisting_only(
        self,
        worktree: Path,
        cmd: str,
        env: dict[str, str],
        head_proc: subprocess.CompletedProcess[str],
    ) -> tuple[bool, frozenset[str]]:
        """Re-run a failed command against the base tree to attribute failures.

        Stashes the agent's uncommitted edits, re-runs *cmd*, then restores them,
        so failures already present without this change do not block. Returns
        ``(is_preexisting_only, newly_introduced_ids)``. Falls back to a
        conservative block (``False``) when no base can be established (a clean
        tree, a stash failure, or any git error), preserving prior behavior.
        """
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
            )
            if not status.stdout.strip():
                return False, frozenset()
            stash = subprocess.run(
                ["git", "stash", "push", "--include-untracked", "--quiet"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
            )
            if stash.returncode != 0:
                return False, frozenset()
            try:
                base_proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(worktree),
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )
            finally:
                subprocess.run(
                    ["git", "stash", "pop", "--quiet"],
                    cwd=str(worktree),
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except Exception:
            return False, frozenset()

        head_ids = _parse_failed_ids(head_proc.stdout, head_proc.stderr, head_proc.returncode)
        base_ids = _parse_failed_ids(base_proc.stdout, base_proc.stderr, base_proc.returncode)
        if head_ids is not None and base_ids is not None:
            new = head_ids - base_ids
            return (not new), new
        # No parseable node-ids (lint, a collection crash). Fall back to exit codes.
        if base_proc.returncode == 0:
            return False, frozenset()
        return True, frozenset()
