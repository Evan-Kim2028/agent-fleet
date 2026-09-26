"""Carrying out a routing decision: the rebase and repair agents.

The policy in :mod:`agent_fleet.routing.policy` says *what*; this module does it.
Two actions need an agent, and both work the same way: a worktree on the PR's
``headRefName``, one engine agent inside it, then commit and push back to that
ref so the next gate run sees the result.

    rebase  — merge origin/<base> in and resolve the conflict, keeping both sides
    repair  — make the PR's own tests runnable, without weakening them

Two rules are load-bearing and are enforced here rather than asked of the model:

* **Never ``--no-verify``.** The commit goes through
  :func:`agent_fleet.fleet_ops.guarantee.commit_worktree`, whose git runner
  raises on ``--no-verify``/``-n``. A hook that fails on baseline debt outside the
  diff is skipped by *name* with ``SKIP=<hook-id>``, and the failing ids are
  reported — never by disabling the whole hook set.
* **Repair never weakens assertions.** A test made to pass by deleting the
  assertion is how a repair turns a caught defect into a silent one, so the
  prompt fences it and the escalation line says which mode ran.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.routing.counters import RoutingError

if TYPE_CHECKING:
    from agent_fleet.hooks import LLMBackend

#: Written to the status file after a successful push. Fail-closed on purpose:
#: the routing policy classifies a fresh ``fail-closed`` as infra and re-gates,
#: so a rebase or repair can never *merge* anything — it can only put the PR back
#: in front of the gate, which has to approve the new head from scratch.
NEEDS_ESCALATION = "NEEDS-ESCALATION"

#: Gate-written tests whose names collide on merge, and how to resolve it.
GATE_TEST_PREFIX = "test_gate_"
GATE_TEST_SUFFIX = ".py"

#: Default budget for the one agent run, matching the gate's fixer budget.
DEFAULT_AGENT_TIMEOUT_S = 1800


class Mode(enum.StrEnum):
    """Which of the two agent-backed actions to run."""

    REBASE = "rebase"
    REPAIR = "repair"


@dataclass(frozen=True)
class AgentResult:
    """What one rebase/repair run did, whether or not the agent succeeded."""

    mode: Mode
    ok: bool
    head: str
    pushed: bool = False
    status_line: str = ""
    detail: str = ""
    worktree: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "ok": self.ok,
            "head": self.head,
            "pushed": self.pushed,
            "status_line": self.status_line,
            "detail": self.detail,
            "worktree": self.worktree,
        }


def resolve_lane(head_ref: str) -> str:
    """The lane slug folded from a PR's head ref.

    ``fb/gate-routing`` -> ``gate-routing``. The lane is the counter key, so
    two pushes to the same branch share a budget and two branches do not.
    """
    return head_ref.strip().removeprefix("fb/").strip("/") or head_ref.strip() or "lane"


def _git(args: list[str], *, cwd: Path) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if result.returncode != 0:
        raise RoutingError(
            f"git {' '.join(args)} exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:300]}"
        )
    return (result.stdout or "").strip()


def prepare_pr_worktree(repo_path: Path, *, head_ref: str) -> Path:
    """A worktree on the PR's *headRefName*, reset to the PR's real head.

    A worktree on the real branch rather than a detached checkout at a sha: the
    result has to be pushed back to ``headRefName``, and a detached HEAD cannot
    name the ref it wants to update without also being wrong about where it
    started. Reuse is fine — an existing worktree on the branch is where a
    previous attempt left its work — but it is reset to the remote head first, so
    a run never builds on a previous attempt's half-merge.
    """
    from agent_fleet.fleet_ops.worktree import ensure_lane_worktree

    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise RoutingError(f"repo path does not exist: {root}")
    _git(["fetch", "--quiet", "origin", head_ref], cwd=root)
    result = ensure_lane_worktree(root, lane=resolve_lane(head_ref), branch=head_ref)
    _git(["reset", "--hard", f"origin/{head_ref}"], cwd=result.path)
    return result.path


def _conflict_rule(lane: str) -> str:
    """The instruction for the ``test_gate_*.py`` add/add collision.

    Both sides wrote a gate test with the same name — the exact collision the
    lane slug was added to prevent, reappearing when base and PR both add one.
    Base's file wins and the PR's is renamed, because base's copy is the one
    every *other* lane's rebase will meet too: renaming base's instead would
    resolve this PR by breaking the next one.

    The name shown is the one this lane would actually produce, so the agent
    renames to the same string the gate will later look for rather than to a
    shape it has to infer.
    """
    from agent_fleet.gate.prompts import lane_slug_token

    token = lane_slug_token(lane) or "x"
    return (
        "Gate test files named `test_gate_*.py` collide when base and this PR both "
        "add one. Resolve that collision by KEEPING BASE'S VERSION and RENAMING "
        f"THIS PR'S COPY to `test_gate_{token}_<suffix>.py` (same directory, same "
        "test bodies, this lane's token added to the name). Never delete either "
        "side's test and never merge two different tests into one file — both must "
        "survive, because each proves a confirmed defect."
    )


def build_prompt(
    mode: Mode,
    *,
    pr_number: int,
    head_ref: str,
    base_ref: str,
    worktree: Path,
    lane: str,
) -> str:
    """The single agent prompt for one rebase or repair round.

    Both modes merge the base in first. A repair that did not would only fix the
    tests against a head that base is about to invalidate, and the merged-tree
    check would fail on the next gate run — the same dead end, one round later.
    """
    from agent_fleet.gate.prompts import AGENT_RULES

    common = (
        f"{AGENT_RULES}"
        f"You are working in {worktree}, a git worktree on branch {head_ref} for PR "
        f"#{pr_number} (base {base_ref}).\n\n"
    )
    steps = (
        f"1. `git fetch origin {base_ref}` then `git merge origin/{base_ref}` into "
        f"{head_ref}. The worktree is already on {head_ref} and at its current head.\n"
    )
    if mode is Mode.REBASE:
        task = (
            f"Merge origin/{base_ref} into {head_ref} and resolve every conflict so "
            "BOTH sides survive. Never resolve a conflict by taking one side whole: "
            "keep this PR's behaviour and base's newer behaviour, in combination. "
            f"{_conflict_rule(lane)}\n"
        )
    else:
        task = (
            f"Make this PR's own tests runnable, then make them pass. Merge "
            f"origin/{base_ref} in first, as above.\n"
            "HARD RULE: never weaken an assertion. Do not delete or skip a test, do "
            "not narrow an `xfail`/`skip` to make it green, do not lower a threshold, "
            "and do not change what a test asserts about product behaviour. If a test "
            "cannot run at all (missing import, missing fixture, missing dependency, "
            "collection error), fix the *setup* — the import, the fixture, the "
            "dependency — so the assertion runs unchanged. If a test genuinely "
            "asserts something the product does not do, that is a product bug: fix "
            "the product code, never the assertion.\n"
            f"{_conflict_rule(lane)}\n"
        )
    tail = (
        "2. Run the tests you touched (memory-capped: `pytest <files>`). Every other "
        "test currently passing must keep passing.\n"
        "3. `git add -A` and commit — NEVER `git commit --no-verify`, and never "
        "disable hooks. If a hook fails on baseline debt outside your diff, re-run "
        "that one commit with `SKIP=<hook-id>` naming only that hook, and say which "
        "hook and why in your final message.\n"
        f"4. `git push origin HEAD:{head_ref}`.\n"
        "Final message: the new head sha, the tests you ran with their results, and "
        "any hook you skipped.\n"
    )
    return f"{common}{steps}{task}{tail}"


def _status_line(mode: Mode) -> str:
    from agent_fleet.fleet_ops.statusfile import escalation_line

    return escalation_line(f"fail-closed: {mode.value} agent pushed; re-gate")


def _commit_or_reuse(
    worktree: Path, *, mode: Mode, lane: str
) -> tuple[bool, str | None, str, list[str]]:
    """Commit the agent's work, or accept the commit the agent already made.

    The agent's own prompt tells it to ``git add -A`` and commit, so on the normal
    success path the worktree is *already clean* when this runs. Committing it
    again is not a no-op: git exits 1 with "nothing to commit, working tree
    clean", and reading that as a failure reported every successful rebase and
    repair as one that did not complete — nothing pushed, no escalation line
    written, exit 1. So the commit is only attempted when there is something to
    commit, and a clean tree with a real HEAD is the agent having done its part.

    The three returns are :func:`commit_worktree`'s, so the caller reads one
    shape either way.
    """
    from agent_fleet.fleet_ops.guarantee import commit_worktree, head_sha, is_dirty

    if is_dirty(worktree):
        return commit_worktree(worktree, engine=mode.value, lane=lane)
    sha = head_sha(worktree)
    if sha is None:
        return False, None, f"{mode.value} agent left the worktree at no commit at all", []
    return True, sha, "", []


def run_agent(
    mode: Mode,
    *,
    repo_path: Path,
    pr_number: int,
    status_file: Path | None = None,
    backend: LLMBackend | None = None,
    model: str | None = None,
    timeout_s: int = DEFAULT_AGENT_TIMEOUT_S,
    head_ref: str = "",
    head: str = "",
) -> AgentResult:
    """Run one rebase or repair agent and push its result to the PR's head.

    *head_ref* overrides the ref ``gh`` reports, and *head* the sha the counters
    were keyed on. The caller has to resolve both anyway to enforce the
    once-per-head budget, and re-resolving here could disagree with the count it
    just charged — so the values it used are carried through instead.

    Fails closed. If the worktree cannot be made, the agent dies, or the push
    does not land, this returns ``ok=False`` and writes no escalation line: the
    routing policy then sees no new verdict and will decide again on the old
    one, rather than believing a half-finished rebase is a finished one. That
    covers the bare ``RuntimeError`` :func:`ensure_lane_worktree` raises when the
    worktree lock is unavailable or ``git worktree add`` fails, so an operator
    gets the documented ``ok=False`` rather than a traceback.

    On success the status line is the ``fail-closed`` escalation, which routes
    straight back to the gate.
    """
    from agent_fleet.fleet_ops.statusfile import append_status
    from agent_fleet.gate.gitops import resolve_pull_request

    def fail(detail: str, *, worktree: Path | None = None) -> AgentResult:
        return AgentResult(
            mode=mode, ok=False, head=head, detail=detail, worktree=str(worktree or "")
        )

    worktree: Path | None = None
    base_ref = ""
    lane = ""
    try:
        ref = resolve_pull_request(Path(repo_path).expanduser().resolve(), pr_number)
        if not ref.is_open:
            raise RoutingError(f"PR #{pr_number} is {ref.state or 'not open'}; nothing to do")
        head_ref = head_ref or ref.head_ref
        base_ref = ref.base_ref
        if not head_ref or not base_ref:
            raise RoutingError(f"PR #{pr_number} did not report a head/base ref")
        head = head or ref.head_sha or head_ref
        lane = resolve_lane(head_ref)
        worktree = prepare_pr_worktree(Path(repo_path), head_ref=head_ref)
        prompt = build_prompt(
            mode,
            pr_number=pr_number,
            head_ref=head_ref,
            base_ref=base_ref,
            worktree=worktree,
            lane=lane,
        )
        engine = backend or _default_backend()
        result = engine.run(
            prompt,
            max_tokens=0,
            timeout_s=timeout_s,
            cwd=worktree,
            model=model,
            mode="agent",
        )
        if result.exit_code != 0:
            raise RoutingError(
                f"{mode.value} agent exited {result.exit_code}: "
                f"{(result.stderr or result.stdout or '').strip()[:300]}"
            )
    except (RuntimeError, OSError) as exc:
        return fail(str(exc), worktree=worktree)

    # Commit before pushing, and refuse to push a commit that failed its hooks:
    # pushing first would put a tree on the PR's head that no hook ever saw, and
    # the next gate run would judge it. A hook that failed on baseline debt
    # outside the diff is retried by name with SKIP=<hook-id>, never disabled.
    committed, sha, detail, hooks_failed = _commit_or_reuse(worktree, mode=mode, lane=lane)
    if not committed:
        return fail(detail or f"{mode.value} agent left nothing to commit", worktree=worktree)
    if hooks_failed:
        return fail(
            f"hooks failed and were not skipped: {', '.join(hooks_failed)}; {detail}",
            worktree=worktree,
        )
    try:
        _git(["push", "origin", f"HEAD:{head_ref}"], cwd=worktree)
    except (RoutingError, OSError) as exc:
        return fail(f"push to {head_ref} failed: {exc}", worktree=worktree)

    line = _status_line(mode)
    if status_file is not None:
        append_status(status_file, line)
    return AgentResult(
        mode=mode,
        ok=True,
        head=sha or head,
        pushed=True,
        status_line=line,
        detail=detail,
        worktree=str(worktree),
    )


def _default_backend() -> LLMBackend:
    """The gate's own fix backend, so a routing agent runs on the same engine.

    Read from the gate config rather than the task-dispatch default: this agent
    is fixing what the gate found, and the gate already has a backend and a
    model policy for exactly that role. It also means a rebase or repair is
    covered by the same model allowlist as the fix rounds it stands in for.

    Falls back to the built-in default when the gate section is absent or
    explicitly disabled — a disabled gate is a configuration choice about the
    *gate*, and routing a PR is still meaningful without it.

    ``_load_raw_config`` is the gate's own reader and is imported despite being
    private: the ``gate:`` section has no field on :class:`FleetConfig` (the gate
    reads the raw mapping for that reason), so re-reading and re-deriving it
    here would be a second source of truth for which backend a fix runs on.
    """
    from agent_fleet.gate.config import GateConfig, load_gate_config
    from agent_fleet.gate.pipeline import _load_raw_config, build_gate_backend

    config = load_gate_config(_load_raw_config(None)) or GateConfig()
    return build_gate_backend(config.backend)
