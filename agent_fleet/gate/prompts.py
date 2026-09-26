"""Prompts for the gate's five agent roles.

One module owns the wording of every prompt the gate sends, so the contract the
gate enforces with a JSON schema is stated in exactly one place. Each prompt
ends with the literal JSON block the role must answer with, matching the
``gate_*`` schemas in ``agent_fleet/schemas/``.

Two rules run through all of them, and they are the reason the gate works:

**Blockers only.** A reviewer that reports nits spends a verifier on each one,
and a fixer that receives a list of nits learns to argue with the list rather
than fix the code. An empty findings list is a normal, good outcome.

**Concrete repro.** A claim without a reproducible input/state and an observable
wrong outcome cannot be turned into a test by anyone, including a strong
verifier. The gate's evidence is a failing test, so a claim that cannot become
one is routed to the judge rather than counted as a blocker.

A third rule is machine-facing rather than task-facing, and is prepended to every
prompt as :data:`AGENT_RULES`: :data:`NO_BLOCKING_COMMANDS` (a command that
never returns makes the stage a dead agent) and :data:`PROCESS_SAFETY` (this box
runs many agents; a pattern kill hits all of them). Both are preconditions for
running here at all, so they live in one shared prefix rather than being restated
in each role.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_fleet.contracts.gate import Finding

#: Longest lane slug / finding id kept in a gate test file name. Both are capped
#: so a long branch name cannot produce a path the OS or pytest chokes on.
SLUG_MAX = 24
ID_MAX = 30


def slugify(text: str, *, limit: int) -> str:
    """Fold *text* to a filename-safe token: alphanumerics and underscores only.

    Gate test names are derived from a branch name, so every character a branch
    may legally contain but a filename may not (``.``, ``/``, ``-``) has to be
    folded. Folding to ``_`` rather than dropping keeps distinct branches
    distinct, which is the whole point of putting the lane in the name. Returns
    ``""`` for input that folds away to nothing, so callers choose their own
    fallback.
    """
    folded = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return folded[:limit].strip("_")


def gate_test_name(lane_slug: str, finding_id: str) -> str:
    """The one name a verifier's new test file must have: ``test_gate_<lane>_<id>.py``.

    A verifier writes its test into the *PR's* repository, so two PRs gating
    different branches both produced ``test_gate_contract_1.py`` — an add/add
    conflict that forced a rebase and a full re-gate for every PR after it. The
    lane slug makes the name unique per PR, which is what lets concurrent
    branches each carry their own gate evidence.
    """
    slug = slugify(lane_slug, limit=SLUG_MAX) or "x"
    identifier = slugify(finding_id, limit=ID_MAX) or "x"
    return f"test_gate_{slug}_{identifier}.py"


PROCESS_SAFETY = (
    "PROCESS SAFETY (hard rule, overrides everything else): this machine runs many "
    "other agents and services. NEVER kill processes by name or pattern — no pkill, "
    "killall, `pgrep ... | kill`, `ps | grep | xargs kill`, and no kill of a process "
    "group you did not create. Agent wrappers can carry prompt text in their command "
    "line, so a pattern like `pkill -f pytest` kills other agents. To stop something "
    "you started, record its PID when you start it and kill only that PID; otherwise "
    "leave it running.\n\n"
)

#: A gate agent that never returns is a dead stage: its slot is held, its answer
#: never arrives, and the run spends a whole budget to escalate. Two failure
#: modes, both seen in production — a blocking command that never exits, and a
#: tool invocation that waits forever. The gate worktrees are disposable, so
#: anything slow can be bounded and re-run rather than watched.
NO_BLOCKING_COMMANDS = (
    "NO BLOCKING COMMANDS (hard rule): never run a command that waits forever — "
    "`tail -f`, `journalctl -f`, `watch`, an interactive editor or pager (`less`, "
    "`vim`), a sleep loop with no exit condition, or a server in the foreground. "
    "To watch something, poll with a bounded loop that has a timeout and an exit "
    "condition. Every command you run needs its own timeout.\n\n"
)

#: Preamble shared by every role prompt. Both rules are preconditions for
#: working on this machine at all, so they precede the task rather than being
#: restated inside it.
AGENT_RULES = NO_BLOCKING_COMMANDS + PROCESS_SAFETY


def _example_block(spec: dict[str, object]) -> str:
    """Fence the *spec* as a literal ```json block the model must copy the shape of."""
    return f"```json\n{json.dumps(spec, indent=2)}\n```"


def _blockers_only() -> str:
    return (
        "Report ONLY BLOCKERS: defects that would make merging wrong — incorrect "
        "results, data loss, contract breaks, crashes, security, or production risk "
        "— within your focus. Style, naming, docs polish, refactors, 'could be "
        "cleaner', missing nice-to-haves, and speculative risks are NOT blockers: "
        "omit them entirely. If you find no blockers, return an empty list; that is "
        "a normal, good outcome."
    )


def find_prompt(
    *,
    lens: str,
    focus: str,
    worktree: str,
    base_branch: str,
    head_sha: str,
    pr_number: int,
    task_text: str,
    prior_claims: str = "",
) -> str:
    """Prompt for one lens reviewer: BLOCKERS ONLY, with a repro per claim."""
    prior_block = ""
    if prior_claims.strip():
        prior_block = (
            "\nA previous reviewer loop made these claims (some may be stale, "
            "fixed, or wrong — include one only if you independently confirm it in "
            f"the current code):\n----- PRIOR CLAIMS -----\n{prior_claims}\n"
            "----- END PRIOR -----\n"
        )
    return AGENT_RULES + (
        f"You are a pre-merge reviewer with ONE focus: **{lens}** — {focus}\n"
        f"Repository worktree (read-only for you; do NOT edit, commit or push): "
        f"{worktree}, detached at PR #{pr_number} head {head_sha}. Review ONLY the "
        f"change: `git diff {base_branch}...HEAD` (run it). Read surrounding code "
        "as needed.\n\n"
        f"{_blockers_only()}\n"
        "Each blocker MUST name the exact file/line and a concrete repro: the "
        "input/state and the observable wrong outcome, precise enough that someone "
        "can write a failing test for it.\n"
        f"{prior_block}\n"
        "Task specification (for context; for the spec lens it is the yardstick):\n"
        f"----- TASK -----\n{task_text}\n----- END TASK -----\n\n"
        "Final answer: exactly one fenced json block:\n"
        f"{_example_block(_findings_spec(lens))}\n"
        'Set "testable": false only when the defect cannot be shown by a '
        "unit/integration test run locally (e.g. needs production data volume)."
    )


def _findings_spec(prefix: str) -> dict[str, object]:
    """The findings answer shape, shown to a lens reviewer."""
    return {
        "findings": [
            {
                "id": f"{prefix}-1",
                "file": "path",
                "line": 123,
                "claim": "one sentence defect",
                "repro": "input/state -> wrong observable outcome",
                "testable": True,
            }
        ]
    }


def _finding_spec() -> dict[str, object]:
    return {
        "id": "path",
        "file": "path",
        "line": 1,
        "claim": "one sentence defect",
        "repro": "input/state -> wrong observable outcome",
        "testable": True,
    }


def _verify_spec(test_dir: str, file_name: str) -> dict[str, object]:
    return {
        "verdict": "CONFIRMED|REJECTED|UNTESTABLE",
        "test_file": f"repo-relative path or null ({test_dir}/{file_name})",
        "reason": "one or two sentences of evidence",
    }


def _judge_spec() -> dict[str, object]:
    return {
        "untestable_rulings": [{"id": "...", "real": True, "reason": "..."}],
        "new_blockers": [{**_finding_spec(), "id": "j-1", "line": 1}],
    }


def _recheck_spec() -> dict[str, object]:
    return {"unresolved": [{"id": "...", "reason": "..."}]}


def verify_prompt(
    *,
    finding: Finding,
    worktree: str,
    base_branch: str,
    head_sha: str,
    pr_number: int,
    test_dir_hint: str,
    pytest_cmd_hint: str,
    test_file_name: str,
) -> str:
    """Prompt for one verifier: write exactly one failing test, or refute the claim.

    *test_file_name* is the exact, lane-unique name the test must have. It is
    passed in rather than derived here so the instruction, the run command and
    the answer example cannot disagree about the file's name.
    """
    claim_json = json.dumps(finding.to_dict(), indent=2)
    return AGENT_RULES + (
        f"You verify ONE claimed defect in worktree {worktree} (detached at PR "
        f"#{pr_number} head {head_sha}; the change is `git diff "
        f"{base_branch}...HEAD`).\n"
        f"Claim (JSON):\n{claim_json}\n\n"
        "Your job: PROVE or DISPROVE it with a test. Create exactly ONE new test "
        f"file named {test_file_name} in the existing test directory that fits "
        f"({test_dir_hint}). Do not modify ANY other file, and do not rename this "
        "file: its name is how the gate finds and archives your evidence, and a "
        "fixed name collides with another PR's gate tests. The test must exercise "
        "the real code path and FAIL at the current head because of the claimed "
        "defect (an assertion failure on the wrong behaviour) — not because of "
        "import errors, missing fixtures, network or environment. Run it "
        f"(memory-capped): `{pytest_cmd_hint} {test_file_name}` from the package "
        "directory that owns pyproject.toml.\n"
        "If after honest effort the claim is false (the code behaves correctly), "
        "delete your test file and answer REJECTED with the evidence. If it truly "
        "cannot be shown by a local test, delete the file and answer UNTESTABLE.\n\n"
        "Final answer: exactly one fenced json block:\n"
        f"{_example_block(_verify_spec(test_dir_hint, test_file_name))}"
    )


def judge_prompt(
    *,
    worktree: str,
    base_branch: str,
    head_sha: str,
    pr_number: int,
    confirmed: str,
    untestable: str,
    task_text: str,
) -> str:
    """Prompt for the single judge call: rule on untestable claims + own blocker pass."""
    return AGENT_RULES + (
        f"You are the final pre-merge judge for PR #{pr_number} in worktree "
        f"{worktree} (detached at {head_sha}). Read-only: do not edit, commit or "
        f"push. The change is `git diff {base_branch}...HEAD`.\n"
        f"Evidence so far — confirmed blockers (each has a failing test):\n{confirmed}\n\n"
        "Claims a test could not show (rule on each: is it a real merge blocker in "
        f"the current code?):\n{untestable}\n\n"
        f"Also do ONE pass of your own for BLOCKERS ONLY (wrong results, data "
        "loss, contract breaks, crashes, security, production risk). No "
        "style/nits. Each new blocker needs file/line and a concrete repro.\n"
        f"Task spec (context):\n{task_text}\n\n"
        "Final answer: exactly one fenced json block:\n"
        f"{_example_block(_judge_spec())}"
    )


def recheck_prompt(
    *,
    worktree: str,
    head_sha: str,
    pr_number: int,
    start_sha: str,
    untestable: str,
) -> str:
    """Prompt for the single judge recheck: which untestable blockers remain."""
    return AGENT_RULES + (
        f"Recheck for PR #{pr_number} in worktree {worktree} at {head_sha} "
        f"(read-only; use a fresh `git fetch` and inspect `git diff {start_sha} "
        f"{head_sha}`). These blockers were ruled real earlier and had no test:\n"
        f"{untestable}\n"
        "For each: is it resolved now? Report only unresolved ones.\n\n"
        "Final answer: exactly one fenced json block:\n"
        f"{_example_block(_recheck_spec())}"
    )


def fix_prompt(
    *,
    pr_number: int,
    worktree: str,
    head_sha: str,
    push_branch: str,
    round_number: int,
    failing: str,
    confirmed: str,
    untestable: str,
    all_tests: str,
    pytest_cmd_hint: str,
    task_text: str,
) -> str:
    """Prompt for one fix round: make the tests failing NOW pass, push to the branch."""
    untestable_block = (
        f"Also resolve these confirmed (untestable) defects:\n{untestable}\n"
        if untestable.strip()
        else ""
    )
    return AGENT_RULES + (
        f"Fix PR #{pr_number} in worktree {worktree} (detached at {head_sha}; push "
        f"with `git push origin HEAD:{push_branch}`). Fix round {round_number}.\n"
        "These tests FAIL right now and must pass (each proves a confirmed defect; "
        "gate tests named test_gate_*.py are already in the worktree — keep them, "
        f"never weaken their assertions):\n{failing}\n"
        f"Defect descriptions:\n{confirmed}\n"
        f"{untestable_block}"
        f"Every other listed test currently passes and must keep passing: {all_tests}\n"
        "Rules: change product code until all of the above pass; run them "
        f"(memory-capped: `{pytest_cmd_hint} <files>`, from the package dir). Touch "
        "nothing unrelated. Commit (never --no-verify; SKIP only a named hook that "
        "fails on baseline debt outside your diff) and push. Final message: new "
        f"head sha and test results.\n"
        f"Task spec (context):\n{task_text}"
    )
