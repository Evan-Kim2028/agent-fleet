"""One agent owns a PR end to end, instead of a fresh agent per round.

The verifier/judge/fixer loop re-reads the PR from scratch every round: no
memory of which claim was already argued down, no memory of which test command
actually works in this repo, no memory of the task the lane started from. This
module keeps that context in one file, ``.agent-fleet/pr/<n>/notes.md``, and
spends a single engine call per round on top of it.

One round is: read the notes, fold the new findings and failing test ids into
them, build ONE fix prompt, run the configured engine once in the lane's
worktree, re-run the PR's tests, append the outcome back to the notes, report
``{pushed, new_head, fixed, disputed, tests}``.

The net loop is unchanged — the gate still re-judges every round — but the
engine is told what the previous rounds already established, so it disputes a
bad claim by citing the earlier round instead of re-deriving it.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_fleet.contracts.gate import Finding, FindingsReport
from agent_fleet.gate.prompts import AGENT_RULES
from agent_fleet.integrations.github_cli import gh

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agent_fleet.backends import LLMBackend
    from agent_fleet.repo import RepoConfig

logger = logging.getLogger(__name__)

__all__ = [
    "PrOwnership",
    "build_fix_prompt",
    "load_findings",
    "own_round",
    "pr_head",
    "pr_notes_path",
    "read_notes",
    "read_task_spec",
    "render_round",
    "run_own",
    "write_notes",
]

#: How much of the prior history travels back into the prompt. The notes grow
#: every round, so an unbounded splice would eventually crowd out the findings
#: that round actually has to act on.
NOTES_HISTORY_CHARS = 6000

#: What the engine is asked to end its answer with. Read back out of stdout to
#: tell "this round fixed nothing" from "this round fixed three things", so it
#: is a fenced JSON block: the only shape a model reproduces reliably.
ANSWER_TAG = "AGENT_FLEET_PR_OWNER_ANSWER"

ANSWER_SPEC: dict[str, Any] = {
    "fixed": ["<finding id>"],
    "disputed": [{"id": "<finding id>", "why": "<why the claim does not hold>"}],
}

NOTHING_TO_DO = "There is nothing to fix this round."


# ---------------------------------------------------------------------------
# The notes file — the memory that carries between rounds
# ---------------------------------------------------------------------------


def pr_notes_path(repo_path: Path, pr_number: int) -> Path:
    """Where PR *pr_number*'s ownership notes live: ``.agent-fleet/pr/<n>/notes.md``."""
    return Path(repo_path) / ".agent-fleet" / "pr" / str(pr_number) / "notes.md"


def read_notes(repo_path: Path, pr_number: int) -> str:
    """The notes so far, or ``""`` for a PR no owner has touched."""
    path = pr_notes_path(repo_path, pr_number)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_notes(repo_path: Path, pr_number: int, text: str) -> Path:
    """Persist *text* as the PR's notes, creating the directory."""
    path = pr_notes_path(repo_path, pr_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else f"{text}\n", encoding="utf-8")
    return path


def _seed(pr_number: int, head: str, task_spec: str, test_command: str) -> str:
    """The notes a first round starts from. Seeded from the task file, because
    the spec is the one thing no earlier round can recover on its own."""
    sections = [
        f"# PR #{pr_number} ownership notes",
        "",
        f"Head at first round: `{head}`",
        "",
        "## Task spec",
        "",
        task_spec.strip() or "(none supplied — ask before changing intent)",
        "",
        "## Test commands that work",
        "",
        f"- `{test_command}`",
        "",
        "## Rounds",
        "",
    ]
    return "\n".join(sections)


def render_round(
    *,
    head: str,
    findings: Sequence[Finding],
    fixed: Sequence[str] = (),
    disputed: Sequence[dict[str, str]] = (),
    tests_ok: bool = True,
    failing: Sequence[str] = (),
    new_head: str = "",
    timestamp: str | None = None,
) -> str:
    """The markdown for one round's outcome, ready to append to the notes.

    Bounded: a round records what changed about the *findings*, not the diff.
    A verdict that flipped is the one thing the next round must not re-litigate
    by accident, so it is written down even when the outcome is "nothing to do".
    """
    stamp = timestamp or datetime.now(UTC).isoformat(timespec="seconds")
    head_note = f" → `{new_head}`" if new_head else ""
    lines = [
        f"### Round {stamp}",
        "",
        f"- head: `{head}`{head_note}",
        f"- findings in: {len(findings)}",
        f"- fixed: {', '.join(fixed) if fixed else 'none'}",
    ]
    if disputed:
        detail = "; ".join(f"{d.get('id', '?')}: {d.get('why', '')}" for d in disputed)
        lines.append(f"- disputed: {detail}")
    else:
        lines.append("- disputed: none")
    lines.append(f"- tests: {'pass' if tests_ok else 'FAIL'}")
    if failing:
        lines.append(f"- failing test ids: {', '.join(failing)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Findings and the PR head
# ---------------------------------------------------------------------------


def load_findings(path: Path | None) -> list[Finding]:
    """Read a findings JSON file, or return ``[]`` when no file was given.

    Accepts the gate's own dialect (a ``{"findings": [...]}`` report) and a bare
    top-level list, so a caller can pipe either shape in.
    """
    if path is None:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, list):
        raw = {"findings": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a findings object or list, got {type(raw).__name__}")
    return FindingsReport.from_dict(raw).findings


def pr_head(pr_number: int, repo_path: Path) -> tuple[str, str]:
    """``(head_ref, head_oid)`` for a PR.

    One ``gh pr view`` for both fields: the branch to work on and the sha we
    must not race both come from the same snapshot, so a push landing between
    two calls cannot leave the owner working a branch that is no longer the head.
    """
    result = gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "headRefName,headRefOid",
        cwd=repo_path,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view {pr_number} failed in {repo_path}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh pr view {pr_number} returned non-JSON: {result.stdout[:200]}"
        ) from exc
    return str(payload.get("headRefName") or ""), str(payload.get("headRefOid") or "")


# ---------------------------------------------------------------------------
# The one prompt, and the one answer
# ---------------------------------------------------------------------------


def build_fix_prompt(
    *,
    pr_number: int,
    head: str,
    branch: str,
    worktree: str,
    findings: Sequence[Finding],
    failing: Sequence[str] = (),
    prior_notes: str = "",
    test_command: str = "",
) -> str:
    """Assemble the single prompt for one ownership round.

    Findings are numbered rather than listed bare, because the answer is keyed
    by the same numbers: a model that has to echo a finding's id string back
    will misspell or truncate it, and a misspelled id reads as "fixed nothing".
    """
    lines = [
        AGENT_RULES.rstrip(),
        "",
        f"PR #{pr_number}, branch `{branch}`, worktree {worktree}, head `{head}`.",
        "",
        "You are the OWNER of this PR. You have been fixing it across several "
        "rounds and the notes below are your own memory of the earlier ones. Fix "
        "everything listed here in one pass, then commit and push.",
        "",
        "## Findings to address",
        "",
    ]
    if findings:
        for index, finding in enumerate(findings, start=1):
            lines.extend(
                [
                    f"{index}. [{finding.id}] {finding.file}:{finding.line}",
                    f"   claim: {finding.claim}",
                    f"   repro: {finding.repro}",
                    "",
                ]
            )
    else:
        lines.extend([NOTHING_TO_DO, ""])

    if failing:
        lines.append("## Tests failing right now")
        lines.append("")
        lines.extend(f"- {test_id}" for test_id in failing)
        lines.append("")

    if prior_notes.strip():
        lines.append("## Your notes from the earlier rounds")
        lines.append("")
        lines.append(f"(the most recent {NOTES_HISTORY_CHARS} characters)")
        lines.append("")
        lines.append(prior_notes[-NOTES_HISTORY_CHARS:])
        lines.append("")

    lines.extend(
        [
            "## Rules",
            "",
            "- Fix the product code so every finding above is genuinely addressed. "
            "Do not weaken a test to make a finding go away.",
            "- If a finding is wrong, say so in the answer below instead of editing "
            "code to satisfy it. A disputed claim is carried into the next round, "
            "so state the reason, not just the disagreement.",
            f"- Verify with: `{test_command}` (run it from the worktree root, memory-capped).",
            "- Touch nothing unrelated. Never `git reset --hard`, `git checkout --`, "
            "`git clean`, or `git stash` — other lanes share this machine.",
            "- Commit (never `--no-verify`; skip only a named hook that is red on "
            "baseline debt outside your diff) and push to the branch.",
            "",
            "## Answer",
            "",
            f"End your message with exactly one fenced json block tagged {ANSWER_TAG}, "
            "in this shape:",
            "",
            "```json",
            json.dumps(ANSWER_SPEC, indent=2),
            "```",
            "",
            "Report the findings you actually fixed by the numbers above. If you "
            "fixed none, say so — an empty round is a real answer, and reporting it "
            "honestly is what lets the next round move on.",
        ]
    )
    return "\n".join(lines)


def parse_answer(stdout: str) -> dict[str, list[Any]]:
    """Read the tagged answer block back out of engine stdout.

    Tolerates a missing or malformed block by reporting an empty round: a model
    that fixed the findings but garbled the trailer must not be read as having
    fixed nothing and sent round after round.
    """
    marker = stdout.rfind(ANSWER_TAG)
    if marker < 0:
        return {"fixed": [], "disputed": []}
    tail = stdout[marker:]
    open_fence = tail.find("```")
    close_fence = tail.rfind("```")
    if open_fence < 0 or close_fence <= open_fence:
        return {"fixed": [], "disputed": []}
    # Skip past the opening fence *line*: the ```json language marker would
    # otherwise be handed to json.loads and fail every well-formed answer.
    body_start = tail.find("\n", open_fence)
    if body_start < 0 or body_start >= close_fence:
        return {"fixed": [], "disputed": []}
    try:
        payload = json.loads(tail[body_start + 1 : close_fence])
    except json.JSONDecodeError:
        return {"fixed": [], "disputed": []}
    if not isinstance(payload, dict):
        return {"fixed": [], "disputed": []}
    fixed = [str(x) for x in cast("list[Any]", payload.get("fixed") or [])]
    raw_disputed = payload.get("disputed")
    disputed = [
        {"id": str(d.get("id", "")), "why": str(d.get("why", ""))}
        for d in cast("list[Any]", raw_disputed or [])
        if isinstance(d, dict)
    ]
    return {"fixed": fixed, "disputed": disputed}


# ---------------------------------------------------------------------------
# The round
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrOwnership:
    """The result of one ownership round."""

    pushed: bool
    new_head: str
    fixed: list[str]
    disputed: list[dict[str, str]]
    tests: dict[str, Any]
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pushed": self.pushed,
            "new_head": self.new_head,
            "fixed": self.fixed,
            "disputed": self.disputed,
            "tests": self.tests,
            "detail": self.detail,
        }


def _head_oid(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _run_tests(worktree: Path, test_command: str, test_ids: Sequence[str]) -> dict[str, Any]:
    """Re-run the PR's tests and report which of *test_ids* still fail."""
    if not test_command.strip():
        return {"ran": False, "ok": True, "failing": list(test_ids), "detail": "no test command"}
    argv = [*test_command.split(), *test_ids] if test_ids else test_command.split()
    result = subprocess.run(
        argv,
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    failing = _failing_from_output(result.stdout, test_ids)
    return {
        "ran": True,
        "ok": result.returncode == 0 and not failing,
        "failing": failing,
        "exit_code": result.returncode,
        "detail": (result.stdout or result.stderr).strip()[-2000:],
    }


def _failing_from_output(stdout: str, test_ids: Sequence[str]) -> list[str]:
    """Which of the requested test ids pytest reported as failing."""
    failing = []
    for test_id in test_ids:
        if f"FAILED {test_id}" in stdout or f"ERROR {test_id}" in stdout:
            failing.append(test_id)
    return failing


def own_round(
    *,
    repo_path: Path,
    pr_number: int,
    findings: Sequence[Finding] = (),
    failing: Sequence[str] = (),
    task_spec: str = "",
    repo: RepoConfig | None = None,
    backend: LLMBackend | None = None,
    worktree: Path | None = None,
) -> PrOwnership:
    """Run one ownership round for *pr_number* and return what it achieved.

    *backend* and *worktree* are the injection seams: a test passes a fake
    engine and a scratch worktree and never touches the network or git. Omitted,
    the configured backend is built and the PR's own worktree is reused.
    """
    repo_path = Path(repo_path)
    branch, start_oid = pr_head(pr_number, repo_path)
    if not branch:
        return PrOwnership(False, "", [], [], {"ran": False, "ok": True}, "PR has no head branch")

    if worktree is None:
        from agent_fleet.pr_loop.github_ops import checkout_branch

        assert repo is not None, "own_round needs a RepoConfig to locate the worktree"
        worktree = checkout_branch(branch, _lane_worktree(repo_path, branch), repo_root=repo_path)

    test_command = (repo.test_command if repo else None) or ""
    prior = read_notes(repo_path, pr_number)
    if not prior.strip():
        # First round: the seed is what every later round reads back, so it has
        # to hit the disk now, not just feed this round's prompt.
        prior = _seed(pr_number, start_oid, task_spec, test_command or "(none configured)")
        write_notes(repo_path, pr_number, prior)

    prompt = build_fix_prompt(
        pr_number=pr_number,
        head=start_oid,
        branch=branch,
        worktree=str(worktree),
        findings=findings,
        failing=failing,
        prior_notes=prior,
        test_command=test_command or "(unknown — check the repo's .agent-fleet.yaml)",
    )

    if backend is None:
        from agent_fleet.backends import make_backend
        from agent_fleet.config import load_fleet_config

        backend = make_backend(load_fleet_config())
    result = backend.run(
        prompt,
        max_tokens=0,
        timeout_s=1800,
        cwd=worktree,
    )
    if result.exit_code != 0:
        detail = f"engine failed: {(result.stderr or result.stdout).strip()[-500:]}"
        _append(
            repo_path,
            pr_number,
            render_round(
                head=start_oid,
                findings=findings,
                failing=failing,
                new_head="",
                tests_ok=False,
                timestamp=_now(),
            ),
        )
        return PrOwnership(False, start_oid, [], [], {"ran": False, "ok": False}, detail)

    answer = parse_answer(result.stdout)
    new_head = _head_oid(worktree)
    moved = bool(new_head) and new_head != start_oid
    push_detail = ""
    pushed = moved
    if moved:
        push = subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if push.returncode != 0:
            pushed = False
            push_detail = f"push failed: {push.stderr.strip()[-500:]}"

    tests = _run_tests(worktree, test_command, failing)
    # Appended before returning in every case: a round that pushed nothing, or
    # whose push failed, still happened and the next round has to know about it.
    _append(
        repo_path,
        pr_number,
        render_round(
            head=start_oid,
            findings=findings,
            fixed=answer["fixed"],
            disputed=answer["disputed"],
            tests_ok=bool(tests["ok"]),
            failing=cast("list[str]", tests.get("failing") or []),
            new_head=new_head if pushed else "",
            timestamp=_now(),
        ),
    )
    return PrOwnership(
        pushed=pushed,
        new_head=new_head,
        fixed=answer["fixed"],
        disputed=answer["disputed"],
        tests=tests,
        detail=push_detail,
    )


def _lane_worktree(repo_path: Path, branch: str) -> Path:
    from agent_fleet.pr_loop.worktree import resolve_worktree_path

    return resolve_worktree_path(
        branch,
        repo_root=repo_path,
        worktree_base=_worktree_base(repo_path),
    )


def read_task_spec(task_file: str | None) -> str:
    """The task spec text, or ``""`` when no file was given."""
    if not task_file:
        return ""
    return Path(task_file).read_text(encoding="utf-8")


def run_own(
    *,
    repo_path: Path,
    pr_number: int,
    findings_path: str | None = None,
    failing: Sequence[str] = (),
    task_file: str | None = None,
) -> dict[str, Any]:
    """One ownership round from raw CLI inputs.

    Reads the repo's own config and calls the configured engine; the testable
    core is :func:`own_round`, which takes both of those already resolved.
    Returns ``{"error": ...}`` rather than raising, so the CLI can print one
    message and exit 1 without catching anything.
    """
    from agent_fleet.repo import resolve_repo_config

    repo_path = Path(repo_path).expanduser().resolve()
    if not repo_path.is_dir():
        return {"error": f"repo path is not a directory: {repo_path}"}
    try:
        findings = load_findings(Path(findings_path) if findings_path else None)
        task_spec = read_task_spec(task_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}

    return own_round(
        repo_path=repo_path,
        pr_number=pr_number,
        findings=findings,
        failing=failing,
        task_spec=task_spec,
        repo=resolve_repo_config(repo_path),
    ).to_dict()


def _worktree_base(repo_path: Path) -> Path:
    from agent_fleet.repo import resolve_repo_config

    repo = resolve_repo_config(repo_path)
    base = repo.worktree_base if repo else None
    return base or (Path(repo_path) / ".worktrees" / "pr")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _append(repo_path: Path, pr_number: int, block: str) -> None:
    base = read_notes(repo_path, pr_number) or _seed(pr_number, "", "", "(none recorded)")
    write_notes(repo_path, pr_number, f"{base.rstrip()}\n\n{block.rstrip()}\n")
