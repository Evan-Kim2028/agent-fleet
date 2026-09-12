"""Command-based verifier driven by repo .agent-fleet.yaml."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity
from agent_fleet.observability.fleet_logger import emit_fleet_event
from agent_fleet.verify_core import get_changed_files_result

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.repo import RepoConfig


@dataclass
class _ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _format_failure(headline: str, proc: _ProcResult) -> str:
    detail = (proc.stderr or proc.stdout or "")[-2000:].rstrip()
    if proc.timed_out:
        if not detail:
            return headline
        return f"{headline}\n{detail}"
    if not detail:
        return f"{headline}\nexit={proc.returncode}"
    return f"{headline}\nexit={proc.returncode}\n{detail}"


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.pid is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            proc.kill()


def _run_shell(
    cmd: str,
    *,
    cwd: str,
    env: dict[str, str],
    timeout_s: int,
) -> _ProcResult:
    """Run *cmd* in a new session; kill the whole process group on timeout.

    ``subprocess.run(timeout=...)`` only signals the shell. ``shell=True``
    verify commands (pytest, uv, xdist) spawn grandchildren that would
    otherwise survive as orphans. ``start_new_session=True`` makes the
    shell the process-group leader so ``killpg`` reaps the tree.
    """
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        # TimeoutExpired.stdout/stderr may be bytes or None even under text=True.
        stdout_s = _as_text(stdout) or _as_text(exc.stdout)
        stderr_s = _as_text(stderr) or _as_text(exc.stderr)
        return _ProcResult(-1, stdout_s, stderr_s, timed_out=True)
    code = proc.returncode if proc.returncode is not None else -1
    return _ProcResult(code, stdout or "", stderr or "")


def _check_record(name: str, proc: _ProcResult) -> dict[str, object]:
    return {
        "name": name,
        "passed": (not proc.timed_out) and proc.returncode == 0,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
        "exit_code": proc.returncode,
    }


_INFRASTRUCTURE_EXIT_CODES = frozenset({126, 127})


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
        changed_result = get_changed_files_result(worktree)
        rel_changed = changed_result.files
        checks: list[dict] = []
        timeout_s = self.repo.verify_timeout_s
        verify_env: dict[str, str] = {
            **os.environ,
            "ISSUE_NUMBER": str(task_id),
            "FLEET_PERSONA": persona,
        }
        worktree_s = str(worktree)
        bootstrap_commands = list(self.repo.worktree_bootstrap_commands)
        bootstrap_t0 = time.monotonic()
        bootstrap_exit_code = 0
        bootstrap_fatal_proc: _ProcResult | None = None
        bootstrap_fatal_cmd: str | None = None
        for cmd in bootstrap_commands:
            proc = _run_shell(cmd, cwd=worktree_s, env=verify_env, timeout_s=timeout_s)
            checks.append(_check_record(f"bootstrap: {cmd}", proc))
            if proc.timed_out or proc.returncode != 0:
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
            # (lockfile drift, missing tools, network). A hang here is
            # the same class of environmental failure as a missing
            # toolchain — FATAL so the runner bails instead of burning
            # fix iterations. A *verify* timeout is different: the
            # agent may have just written an infinite loop.
            headline = (
                f"Worktree bootstrap timed out after {timeout_s}s: {bootstrap_fatal_cmd}"
                if bootstrap_fatal_proc.timed_out
                else f"Worktree bootstrap failed: {bootstrap_fatal_cmd}"
            )
            return VerifyResult(
                severity=VerifySeverity.FATAL,
                checks=checks,
                violating_paths=[],
                files_changed=rel_changed,
                message=_format_failure(headline, bootstrap_fatal_proc),
            )

        commands = self.repo.verify_commands_for(persona)
        verify_commands_ran = bool(commands)
        for cmd in commands:
            proc = _run_shell(cmd, cwd=worktree_s, env=verify_env, timeout_s=timeout_s)
            checks.append(_check_record(cmd, proc))
            if proc.timed_out:
                # Hung verify is plausibly an infinite loop in code the
                # agent just wrote. RETRY feeds the fix loop.
                return VerifyResult(
                    severity=VerifySeverity.RETRY,
                    checks=checks,
                    violating_paths=[],
                    files_changed=rel_changed,
                    message=_format_failure(
                        f"Verification timed out after {timeout_s}s: {cmd}",
                        proc,
                    ),
                )
            if proc.returncode != 0:
                if proc.returncode in _INFRASTRUCTURE_EXIT_CODES:
                    return VerifyResult(
                        severity=VerifySeverity.FATAL,
                        checks=checks,
                        violating_paths=[],
                        files_changed=rel_changed,
                        message=_format_failure(
                            f"Verification command not found or not executable: {cmd}",
                            proc,
                        ),
                    )
                # Auto-apply `ruff check --fix` once before counting this as a
                # failure. This prevents the fix loop from burning attempts on
                # import-sorting (I001) and other auto-fixable lint issues.
                # Scope is intentionally narrow — only `ruff check` triggers it.
                if "ruff check" in cmd and "--fix" not in cmd:
                    fix_cmd = cmd + " --fix"
                    _run_shell(fix_cmd, cwd=worktree_s, env=verify_env, timeout_s=timeout_s)
                    rerun_proc = _run_shell(
                        cmd, cwd=worktree_s, env=verify_env, timeout_s=timeout_s
                    )
                    # Count files changed by ruff --fix via git diff --name-only
                    _files_changed_count = 0
                    try:
                        _diff = subprocess.run(
                            ["git", "diff", "--name-only"],
                            cwd=worktree_s,
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
                    record = _check_record(cmd, rerun_proc)
                    record["autofix_applied"] = True
                    checks[-1] = record
                    if rerun_proc.timed_out:
                        return VerifyResult(
                            severity=VerifySeverity.RETRY,
                            checks=checks,
                            violating_paths=[],
                            files_changed=rel_changed,
                            message=_format_failure(
                                f"Verification timed out after {timeout_s}s: {cmd}",
                                rerun_proc,
                            ),
                        )
                    if rerun_proc.returncode == 0:
                        continue
                    proc = rerun_proc
                preexisting, new_ids = self._preexisting_only(
                    worktree, cmd, verify_env, proc, timeout_s=timeout_s
                )
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
            if not changed_result.determinate:
                return VerifyResult(
                    severity=VerifySeverity.RETRY,
                    checks=checks,
                    violating_paths=[],
                    files_changed=[],
                    message=("VERIFY could not determine changed files (indeterminate git state)"),
                )
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
        head_proc: _ProcResult,
        *,
        timeout_s: int,
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
                base_proc = _run_shell(cmd, cwd=str(worktree), env=env, timeout_s=timeout_s)
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
        if base_proc.timed_out:
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
