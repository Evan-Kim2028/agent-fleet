"""Status-file lines and per-operator hooks.

The bash drivers each wrote a status file and both operators' downstream tooling
read it, so the *line format is a contract* and this module is where it lives::

    HH:MM:SS PREMERGE-APPROVED <sha9>
    HH:MM:SS NEEDS-ESCALATION <reason>

Every line is appended, never rewritten — ``automerge.sh`` tails the file and
takes the last matching line, and an operator's monitor greps for the token. The
``HH:MM:SS`` prefix is local wall clock because that is what the operators read.

The approval and escalation *hooks* are per-operator shell commands from config.
They run with a small, explicitly-constructed environment rather than inheriting
the manager's: a hook is operator-authored config, and handing it the whole
manager environment would leak credentials and internal paths into a command the
operator did not write. The documented variables below are the hook's whole
interface.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.fleet_ops.registry import local_hhmmss

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Terminal status tokens. These strings are the contract downstream tools match.
APPROVED_TOKEN = "PREMERGE-APPROVED"
ESCALATION_TOKEN = "NEEDS-ESCALATION"

#: A lane that stopped after guaranteeing its PR because the gate did not run —
#: either ``--no-gate`` or no gate installed. It is neither of the two above:
#: writing it as an escalation made operators read a working lane as a broken
#: one, and made the merge planner's approval sweep treat it as needing a human.
GATE_SKIPPED_TOKEN = "GATE-SKIPPED"

#: Default cap for a hook command. A hook that hangs must not wedge the lane.
DEFAULT_HOOK_TIMEOUT_S = 300


def short_sha(sha: str | None) -> str:
    """The 9-character prefix the status lines carry (matching the bash drivers)."""
    return (sha or "").strip()[:9]


def format_line(token: str, detail: str = "") -> str:
    """One status line, without the trailing newline."""
    suffix = f" {detail}" if detail else ""
    return f"{local_hhmmss()} {token}{suffix}"


def approved_line(sha: str | None) -> str:
    return format_line(APPROVED_TOKEN, short_sha(sha))


def escalation_line(reason: str) -> str:
    return format_line(ESCALATION_TOKEN, (reason or "").strip())


def gate_skipped_line(pr: int | None, *, sha: str | None, reason: str) -> str:
    """``HH:MM:SS GATE-SKIPPED PR #<n> @<sha9> (<reason>)``.

    The PR number and head are in the line because this is the outcome an
    external gate or a merge planner picks up from, and it should not have to
    cross-reference the registry to learn which PR is waiting on it.

    A missing sha contributes no ``@`` field at all rather than a placeholder:
    the old automerge took the trailing field of a status line as a commit id,
    and a literal stand-in there is a sha that matches no PR.
    """
    parts = [GATE_SKIPPED_TOKEN]
    if pr is not None:
        parts.append(f"PR #{pr}")
    head = short_sha(sha)
    if head and all(c in "0123456789abcdef" for c in head.lower()):
        parts.append(f"@{head}")
    clean = " ".join((reason or "gate did not run").split())[:200]
    return f"{local_hhmmss()} {' '.join(parts)} ({clean})"


def append_status(path: Path | str, line: str) -> Path:
    """Append *line* to the status file, creating parent directories.

    Append-only and never truncated: a consumer tailing the file mid-lane must
    still see the earlier lines. A failure to write is logged rather than raised
    — the status file is an observability channel, and losing it must not abort a
    lane that has already done real work.
    """
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError as exc:
        logger.warning("could not write status file %s: %s", target, exc)
    return target


def read_status_lines(path: Path | str) -> list[str]:
    """All status lines currently in the file (empty when it does not exist)."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def last_status_line(path: Path | str, *, tokens: tuple[str, ...] = ()) -> str:
    """The most recent line, optionally restricted to lines containing a token.

    The token filter is what makes this useful for ``lanes status``: a lane's file
    ends with noise (commit counts, gate chatter) but its *verdict* is the last
    line carrying a terminal token.
    """
    lines = read_status_lines(path)
    if tokens:
        for line in reversed(lines):
            if any(token in line for token in tokens):
                return line
        return ""
    return lines[-1] if lines else ""


# --------------------------------------------------------------------- hooks


@dataclass(frozen=True)
class HookResult:
    """Outcome of running one operator hook."""

    ran: bool
    ok: bool
    command: str
    exit_code: int | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ran": self.ran,
            "ok": self.ok,
            "command": self.command,
            "exit_code": self.exit_code,
            "detail": self.detail,
        }


def hook_env(
    *,
    lane: str,
    operator: str,
    repo: str,
    pr: int | None,
    sha: str | None,
    status_file: str,
    verdict: str = "",
    exit_code: int | None = None,
) -> dict[str, str]:
    """The hook's entire environment. Documented, minimal, and explicit.

    ``documents-1d``'s shipper contract is exactly this set: it writes
    ``dq/reviews/<PR>-<sha9>.md`` and appends ``exit=<rc>`` to its monitor log, so
    ``PR`` and ``SHA9`` have to be present and spelled the way it expects.
    """
    env = {
        "LANE": lane,
        "OPERATOR": operator,
        "REPO": repo,
        "PR": str(pr) if pr is not None else "",
        "SHA9": short_sha(sha),
        "STATUS": status_file,
        "VERDICT": verdict,
    }
    if exit_code is not None:
        # `exit` is the name documents-1d's monitor greps for; RC is there so a
        # POSIX-shell hook can read it without quoting an awkward name.
        env["exit"] = str(exit_code)
        env["RC"] = str(exit_code)
    return env


def run_hook(
    command: str | None,
    env: dict[str, str],
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_HOOK_TIMEOUT_S,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> HookResult:
    """Run an operator hook with a clean environment.

    *command* is operator-authored config, so it runs through the shell the way
    the bash drivers' hooks did — but it gets only *env* plus ``PATH``/``HOME``,
    not the manager's environment. A hook that fails is reported, never fatal: the
    lane's verdict is already recorded, and a broken downstream notifier must not
    rewrite it.
    """
    if not command or not command.strip():
        return HookResult(ran=False, ok=True, command="")

    full_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        **env,
    }
    try:
        if runner is not None:
            result = runner(
                command,
                shell=True,
                cwd=cwd,
                env=full_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                env=full_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return HookResult(
            ran=True, ok=False, command=command, detail=f"hook timed out after {timeout}s"
        )
    except OSError as exc:
        return HookResult(ran=True, ok=False, command=command, detail=str(exc))

    exit_code = result.returncode
    ok = exit_code == 0
    # A successful hook's stdout is how it reports what it did ("wrote
    # dq/reviews/3544-abcdef123.md"), and that is the only confirmation the
    # operator gets, so it is kept. Only on failure is stderr folded in, since
    # that is the part that explains what went wrong.
    output = result.stdout if ok else "\n".join(p for p in (result.stdout, result.stderr) if p)
    return HookResult(
        ran=True, ok=ok, command=command, exit_code=exit_code, detail=output.strip()[:1000]
    )


__all__ = [
    "APPROVED_TOKEN",
    "DEFAULT_HOOK_TIMEOUT_S",
    "ESCALATION_TOKEN",
    "GATE_SKIPPED_TOKEN",
    "HookResult",
    "append_status",
    "approved_line",
    "escalation_line",
    "format_line",
    "gate_skipped_line",
    "hook_env",
    "last_status_line",
    "read_status_lines",
    "run_hook",
    "short_sha",
]
