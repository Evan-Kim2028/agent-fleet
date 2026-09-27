"""Per-repo REQUIRED CHECKS: the deterministic commands a diff must survive.

The gate's deterministic half was pytest, and pytest only. A PR that broke dbt
unit-test compilation reviewed clean and merged, because nothing in the gate
knew that repo had a second kind of build. The set of commands that must pass is
a property of the repo, not of the gate, so it is configured per repo
(``gate.required_checks``) rather than hardcoded here.

Two rules make the list safe to rely on, and both are about what a result
*means*:

**A non-zero exit is a confirmed blocker.** No interpretation step, exactly like
a failing test in step0. The command's output tail rides along as the evidence,
so the fixer is handed the same thing a failing test hands it.

**An infra failure is not a verdict.** A command that cannot be found, a timeout,
a crash of the wrapper — none of those are evidence about the code. They fail
*closed* with "check could not run", which is an escalation, never an approval.
The asymmetry is the whole point: a check the gate could not execute must not
read as a check that passed.

Selection is by path. A check runs only when the PR diff touches a path matching
one of its ``when_paths`` regexes, so a docs PR does not pay for a dbt compile.
The default is ``when_paths: ["."]`` — match everything — because a check a repo
configured without scoping it almost certainly meant "always".

The check runs in the gate's worktree at the head under test, so it sees exactly
the tree the review is about. When the repo's gate is rechecked against a new
head, the same checks run again on the merged-with-base tree, because a check
that passes on the PR's own tree and fails once the base is merged in is the
merged-tree regression this exists to catch.

**A check is a memory-hungry subprocess, so it holds a test-pool slot.** A dbt
compile is as capable of eating the machine as a test suite is, and the test
pool exists to bound exactly that across every independent gate process. Every
check holds a slot for the duration of its subprocess, the same guarantee pytest
gets, so a wide fan-out of gate runs queues instead of running every repo build
at once.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agent_fleet.slots import SlotPool

logger = logging.getLogger(__name__)

#: ``{changed_files}`` / ``{changed_models}`` are substituted before the command
#: runs. They are the only substitutions: a check is a repo-authored command, and
#: everything else about it is run as written.
PLACEHOLDERS = ("changed_files", "changed_models")

#: A dbt model is a ``.sql``/``.py`` file anywhere under a ``models/`` directory,
#: named after the file: the last path segment without its extension.
#: ``transform/models/stg_orders/stg_orders.sql`` -> ``stg_orders`` and
#: ``transform/models/marts/daily/daily.sql`` -> ``daily`` alike, because dbt
#: nests models one directory per node and the spec's own glob
#: (``transform/models/**/<name>.sql``) admits the nested shape. This is a
#: filename convention rather than project introspection, so a repo that nests
#: differently still gets the filename — which is more useful to a fixer than
#: the raw path.
_MODEL_RE = re.compile(r"(?:^|/)models/(?:.*/)?([^/]+)\.(?:sql|py)$")

#: How much command output to keep as evidence. A failing compile prints
#: kilobytes; the fixer needs the part that says what broke, not the whole log.
_TAIL_LINES = 40
_TAIL_CHARS = 4000

#: Default budget for a check that does not set its own. Checks are repo commands
#: — a compile, a lint, a migration dry-run — not test suites, so this is sized
#: for a build rather than a review stage.
DEFAULT_CHECK_TIMEOUT_S = 900
DEFAULT_CHECK_MEMORY = "6G"

#: ``when_paths`` default: match every path, so a check with no scoping runs.
MATCH_ALL = (".",)


def changed_model_names(paths: Sequence[str]) -> list[str]:
    """The dbt model names *paths* touches, sorted and de-duplicated.

    Derived from the ``models/<name>/<file>.sql|py`` convention. A path that is
    not a model contributes nothing rather than contributing its filename, so
    ``{changed_models}`` stays a list of model names and never a list of files
    that merely happen to sit near a ``models`` directory.
    """
    names = {m.group(1) for path in paths for m in [_MODEL_RE.search(str(path))] if m}
    return sorted(names)


def _tail(*streams: str) -> str:
    """The last few lines of a command's output, for use as blocker evidence."""
    text = "\n".join(s for s in streams if s)
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-_TAIL_LINES:])[-_TAIL_CHARS:]


@dataclass(frozen=True)
class RequiredCheck:
    """One repo-configured command the gate must be able to run and pass.

    ``name`` is how the check is reported, in metrics, in the blocker claim and
    in the reason line, so it is worth making one that says what broke
    ("dbt-compile") rather than which lane wrote it.
    """

    name: str
    command: str
    #: Regexes matched (with ``re.search``) against repo-relative changed paths.
    when_paths: tuple[str, ...] = MATCH_ALL
    timeout_s: int = DEFAULT_CHECK_TIMEOUT_S
    memory: str = DEFAULT_CHECK_MEMORY

    def matches(self, changed: Sequence[str]) -> bool:
        """Whether this check is selected by *changed*.

        A path that matches any pattern selects the check. A check with an empty
        ``when_paths`` is treated as match-all rather than match-nothing: a
        half-written entry in a config must not silently stop being enforced.

        An empty *diff* selects nothing, whatever the patterns say. There is no
        change for any check to speak to, and the empty list is just as likely
        to be a failed ``git diff`` as a PR that touched no file — so running
        the whole set against it would either burn a budget for no evidence or,
        worse, report a red check for code the PR never touched.

        A malformed ``when_paths`` regex is a config error, not a code fact, and
        must not decide a verdict. Rather than letting ``re.search`` raise out
        of the gate, a bad pattern makes the check *selected* so that it is
        reached and reported: it is then surfaced as ``could_not_run`` (see
        :meth:`bad_patterns` and :func:`run_checks`), which the pipeline already
        fails closed on. Silently skipping the pattern would read a misconfigured
        check as "not applicable" and let the PR through — the exact failure this
        feature exists to prevent.
        """
        if not changed:
            return False
        if self.bad_patterns():
            return True
        patterns = self.when_paths or MATCH_ALL
        return any(re.search(pattern, str(path)) for path in changed for pattern in patterns)

    def bad_patterns(self) -> list[str]:
        """The ``when_paths`` entries that are not valid regexes.

        Empty for a well-formed check. Used to turn a config typo into the same
        fail-closed "could not run" the gate already reports for a missing
        binary, instead of an uncaught :class:`re.error` that kills the run with
        a traceback.
        """
        patterns = self.when_paths or MATCH_ALL
        bad: list[str] = []
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                bad.append(f"{pattern!r} ({exc})")
        return bad


@dataclass(frozen=True)
class CheckResult:
    """The outcome of running one :class:`RequiredCheck` at one tree.

    ``could_not_run`` is the load-bearing field. A check that failed tells us
    something about the code; a check that could not run tells us nothing, and
    the two must never be collapsed into a single boolean, or a missing binary
    would merge as a passing check.
    """

    name: str
    command: str
    #: Where it ran: ``"head"`` or ``"merged"``.
    stage: str
    returncode: int
    duration_s: float
    evidence: str = ""
    could_not_run: bool = False
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.could_not_run

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "stage": self.stage,
            "returncode": self.returncode,
            "duration_s": round(self.duration_s, 3),
            "passed": self.passed,
            "could_not_run": self.could_not_run,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def select_checks(checks: Sequence[RequiredCheck], changed: Sequence[str]) -> list[RequiredCheck]:
    """The checks *changed* selects, in configured order."""
    return [check for check in checks if check.matches(changed)]


def _render(command: str, *, changed_files: Sequence[str], changed_models: Sequence[str]) -> str:
    """Substitute the two documented placeholders into *command*.

    Only ``{changed_files}`` and ``{changed_models}`` are substituted, and only as
    whole words, so a shell brace expansion in a repo's own command (``{1..3}``)
    is not silently rewritten. The values are whitespace-joined and never
    quoted here: the check author decides how to quote them, and a value that
    reached the shell unquoted is the config's command, run as written.

    The substitution is done with a *function* replacement, not a string, so the
    value is inserted literally. A string replacement is a template: a backslash
    in a filename is legal on Linux and ``models/a\\1b.sql`` would be read as a
    backreference and raise :class:`re.error` — crashing the gate over an
    ordinary repo path.
    """
    values = {
        "changed_files": " ".join(changed_files),
        "changed_models": " ".join(changed_models),
    }
    rendered = command
    for key in PLACEHOLDERS:
        rendered = re.sub(rf"\{{{key}\}}", lambda _m, v=values[key]: v, rendered)
    return rendered


def _memory_wrapper(command: list[str], memory: str, use_systemd: bool) -> list[str]:
    """Prefix *command* with the same memory cap every pytest gets.

    A required check is an arbitrary repo command and is exactly as capable of
    eating the machine as a test suite is, so the cap applies to it too. The cap
    is used as configured and never raised.
    """
    if not use_systemd:
        return command
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        f"MemoryMax={memory}",
        "-p",
        "MemorySwapMax=0",
        *command,
    ]


def run_check(
    check: RequiredCheck,
    *,
    worktree: Path,
    stage: str,
    changed_files: Sequence[str],
    changed_models: Sequence[str],
    use_systemd: bool = False,
    pool: SlotPool | None = None,
) -> CheckResult:
    """Run *check* in *worktree* and classify the outcome.

    Never raises. The two failure modes are kept apart on purpose: a non-zero
    exit is a confirmed blocker with the command's tail as evidence, and anything
    that prevented the command from running at all is ``could_not_run``. A
    malformed command is in the second class — the gate cannot tell a typo in a
    config from a missing tool, and neither is evidence about the PR.

    *pool* is the machine-wide test pool. A check is an arbitrary repo command
    and exactly as capable of eating the machine as a test suite, so the check
    holds one slot for the whole of its subprocess — held *while the command
    runs*, not merely while the argv is built, which is the only window in which
    the budget means anything. ``None`` means the caller supplied no pool, and
    the run is then bounded only by the memory cap.
    """
    bad = check.bad_patterns()
    if bad:
        # A malformed when_paths regex is a config typo, not a fact about the
        # code. It cannot be ignored (that would read as "not selected" and let
        # the PR through) and it is not a red check, so it is reported as the
        # infra failure it is — the same fail-closed path a missing binary
        # takes, with no uncaught re.error escaping the gate.
        return CheckResult(
            name=check.name,
            command=check.command,
            stage=stage,
            returncode=127,
            duration_s=0.0,
            could_not_run=True,
            reason=f"invalid when_paths regex: {'; '.join(bad)}",
        )
    rendered = _render(check.command, changed_files=changed_files, changed_models=changed_models)
    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        return CheckResult(
            name=check.name,
            command=rendered,
            stage=stage,
            returncode=127,
            duration_s=0.0,
            could_not_run=True,
            reason=f"command could not be parsed: {exc}",
        )
    if not argv:
        return CheckResult(
            name=check.name,
            command=rendered,
            stage=stage,
            returncode=127,
            duration_s=0.0,
            could_not_run=True,
            reason="command is empty after placeholder substitution",
        )

    argv = _memory_wrapper(argv, check.memory, use_systemd)
    logger.debug("gate check %s (%s): %s", check.name, stage, rendered)
    guard = pool.slot(timeout_s=None) if pool is not None else contextlib.nullcontext()
    try:
        with guard:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=worktree,
                timeout=check.timeout_s,
                check=False,
            )
    except subprocess.TimeoutExpired:
        # A timeout is an infra failure. The check may well be failing, but the
        # gate cannot tell that from a check that hangs, so it does not claim to.
        return CheckResult(
            name=check.name,
            command=rendered,
            stage=stage,
            returncode=124,
            duration_s=float(check.timeout_s),
            could_not_run=True,
            reason=f"check timed out after {check.timeout_s}s",
        )
    except OSError as exc:
        # ENOENT and friends: the check never started, so there is no result.
        return CheckResult(
            name=check.name,
            command=rendered,
            stage=stage,
            returncode=127,
            duration_s=0.0,
            could_not_run=True,
            reason=f"check could not run: {exc}",
        )
    return CheckResult(
        name=check.name,
        command=rendered,
        stage=stage,
        returncode=completed.returncode,
        duration_s=float(getattr(completed, "duration_s", 0.0) or 0.0),
        evidence=_tail(completed.stdout or "", completed.stderr or ""),
    )


def run_checks(
    checks: Sequence[RequiredCheck],
    *,
    worktree: Path,
    stage: str,
    changed_files: Sequence[str],
    use_systemd: bool = False,
    pool: SlotPool | None = None,
) -> list[CheckResult]:
    """Select by *changed_files*, then run every selected check against one tree.

    Selection lives here rather than at the call site so that a caller cannot
    run a check the diff did not select by forgetting to filter — the selection
    and the execution are one decision, and the one that has to be right.

    *pool* is handed to each check in turn, so the whole sequence is bounded by
    the same machine-wide budget a test suite is.

    Sequential rather than parallel: a repo's checks are usually a compile and a
    lint, both of which want the same CPU and the same memory, and the gate has
    no way to know they are safe to overlap. Ordering is also what makes a
    multi-check run's output readable — the first failing check is the one named
    in the reason, not whichever happened to finish last.
    """
    models = changed_model_names(changed_files)
    return [
        run_check(
            check,
            worktree=worktree,
            stage=stage,
            changed_files=changed_files,
            changed_models=models,
            use_systemd=use_systemd,
            pool=pool,
        )
        for check in select_checks(checks, changed_files)
    ]


def blocker_claim(result: CheckResult) -> str:
    """The one-line claim a failing check contributes to the blocker list.

    Shaped like a step0 failing-test claim so the fixer receives both the same
    way: what broke, and the command's own words about it.
    """
    where = "merged with base" if result.stage == "merged" else "head"
    return f"required check '{result.name}' failed at {where}: {result.command}"


def cannot_run_claim(result: CheckResult) -> str:
    """The fail-closed reason for a check that never produced a result."""
    where = "merged with base" if result.stage == "merged" else "head"
    return f"required check '{result.name}' could not run at {where}: {result.reason}"


def to_evidence(result: CheckResult) -> dict[str, Any]:
    """The confirmed-blocker row a failing check contributes.

    ``source`` is ``required-check`` so a reader (and the metrics rollup) can tell
    a command that failed from a test that failed, while the fixer treats both
    identically: a claim with a repro and evidence attached.
    """
    return {
        "id": f"RC-{result.name}",
        "source": "required-check",
        "claim": blocker_claim(result),
        "test_id": None,
        "test_file": None,
        "lens": "required-check",
        "command": result.command,
        "stage": result.stage,
        "returncode": result.returncode,
        "evidence": result.evidence,
    }


__all__ = [
    "DEFAULT_CHECK_MEMORY",
    "DEFAULT_CHECK_TIMEOUT_S",
    "CheckResult",
    "RequiredCheck",
    "blocker_claim",
    "cannot_run_claim",
    "changed_model_names",
    "run_check",
    "run_checks",
    "select_checks",
    "to_evidence",
]
