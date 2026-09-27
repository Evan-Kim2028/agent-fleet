"""The ``gate`` pipeline: evidence-based pre-merge approval for an open PR.

The gate answers one question — *is this PR safe to merge?* — and answers it with
evidence rather than opinion. A claim only becomes a blocker once a test
demonstrates it, and the only thing the gate ever approves is a head where the
deterministic test set is green.

The steps, in order:

0. **step0** — run the PR's own changed test files at head. Every failure is a
   confirmed blocker with no interpretation step at all. A pytest exit >= 2 is an
   *infra* error (collection crash, usage error, timeout), never a finding: we
   learned nothing about the code, so the run escalates rather than guessing.
1. **find** — N lens reviewers in parallel, blockers only, each with file/line and
   a concrete repro.
2. **verify** — one verifier per claim, which must write exactly one new failing
   test. The pipeline re-runs that test itself and counts it only if pytest exits
   1 (a test failure). A verifier that says CONFIRMED but whose test passes is
   discarded: the pipeline trusts the test, not the verdict.
3. **judge** — at most one call on the separately configured judge backend, which
   rules on claims no local test could show and does one blocker pass of its own.
   The judge's new claims go back through verify, so the judge cannot assert a
   blocker either.
4. **converge** — fix rounds continue while the failing set *strictly shrinks and
   no new failures appear*. ``max_fix_rounds`` is a safety net, not the rule.
5. **outcome** — ``APPROVED`` with a sha, or ``NEEDS_ESCALATION`` with reasons.

The convergence rule is the load-bearing design choice. A fixed round cap either
gives up on a PR that needed two rounds (a false escalation) or keeps fixing a
PR that will never converge (a wasted budget and a merge nobody can trust).
Measuring progress instead — did the failing set shrink, and did anything new
break — lets the loop stop on evidence: zero failing is approval, no measurable
progress is escalation, with the per-round numbers recorded either way.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_fleet.backends import make_backend
from agent_fleet.contracts.gate import (
    Finding,
    FindingsReport,
    GateOutcome,
    JudgeReport,
    RecheckReport,
    VerifyReport,
    VerifyVerdict,
    validate_findings,
    validate_judge,
    validate_recheck,
    validate_verify,
)
from agent_fleet.fleet_ops.gate import APPROVAL_MARKER
from agent_fleet.gate import metrics as gate_metrics
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.gitops import (
    GateError,
    PullRequestRef,
    changed_paths,
    changed_test_config_paths,
    changed_test_files,
    current_pr_head,
    deleted_test_paths,
    diff_line_stats,
    fetch_base,
    has_approval_line,
    is_docs_or_test,
    merge_base_into,
    merge_conflict_check,
    patch_id,
    prepare_worktree,
    prodsensitive_paths,
    remove_worktree,
    resolve_diff_base,
    resolve_pull_request,
    worktree_head_sha,
)
from agent_fleet.gate.prompts import (
    ALL_FOCUS,
    ALL_FOCUS_LENS,
    find_prompt,
    fix_prompt,
    gate_test_name,
    judge_prompt,
    recheck_prompt,
    verify_prompt,
)
from agent_fleet.gate.pytest_runner import (
    PytestResult,
    TestPackage,
    build_pytest_command,
    find_test_packages,
    run_pytest,
    systemd_run_available,
    to_package_path,
    to_repo_node_id,
)
from agent_fleet.gate.standard import (
    OUTCOME_APPROVED as STD_OUTCOME_APPROVED,
)
from agent_fleet.gate.standard import (
    OUTCOME_FALLBACK as STD_OUTCOME_FALLBACK,
)
from agent_fleet.gate.standard import (
    OUTCOME_FIX_AND_REGATE as STD_OUTCOME_FIX_AND_REGATE,
)
from agent_fleet.gate.standard import (
    STANDARD_HISTORY_ROWS,
    STANDARD_TIER,
    FallbackReason,
    StandardAction,
    StandardState,
)
from agent_fleet.gate.standard import (
    approval_reason as standard_approval_reason,
)
from agent_fleet.gate.standard import (
    next_action as standard_next_action,
)
from agent_fleet.gate.standard import (
    prior_passes as standard_prior_passes,
)
from agent_fleet.gate.standard import (
    select_tier as standard_select_tier,
)
from agent_fleet.gate.state import STAGE_FIND, STAGE_VERIFY, GateRunState, StageState
from agent_fleet.gate.structured import TIMEOUT_EXIT, StructuredCallError, call_structured
from agent_fleet.model_policy import ModelPolicy, ModelPolicyError, parse_model_policy
from agent_fleet.slots import (
    PoolConfig,
    SlotPool,
    agent_slot_pool,
    default_slots_root,
    test_slot_pool,
)

if TYPE_CHECKING:
    from agent_fleet.agent_mode import AgentMode
    from agent_fleet.hooks import LLMBackend

logger = logging.getLogger(__name__)

ROLE_LENS = "lens"
ROLE_VERIFIER = "verifier"
ROLE_FIX = "fix"
ROLE_JUDGE = "judge"

_NO_TASK_TEXT = "(no task file supplied; judge against the PR description)"


class GateInfraError(RuntimeError):
    """A deterministic step could not run — the gate must not claim a verdict."""


@dataclass(frozen=True)
class ReviewTier:
    """The review tier a PR earned, and the lens set that goes with it.

    ``tier`` is the number of reviewers, and it is the *number* rather than a
    label because the configured lens set is what it counts: a repo that
    configures two lenses gets 2 and a repo that configures four gets 4, and
    both are the "full lens set" tier. The log line is written from these
    numbers so a run's depth can be read after the fact without re-deriving the
    diff.
    """

    tier: int
    lenses: tuple[str, ...]
    #: Non-test changed lines in the PR (see :func:`diff_line_stats`).
    lines: int
    #: Changed paths that matched a production-sensitive pattern.
    risky: list[str] = field(default_factory=list)
    #: Why this tier was chosen, when the default reason does not explain it.
    note: str = ""

    @property
    def summary(self) -> str:
        """The one line that says which tier ran and on what evidence."""
        head = (
            f"review tier: {self.tier} "
            f"(non-test diff {self.lines} lines, {len(self.risky)} production-sensitive files)"
        )
        return f"{head}; {self.note}" if self.note else head


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """The gate's verdict, its evidence, and its convergence trace."""

    outcome: GateOutcome
    sha: str
    reasons: list[str] = field(default_factory=list)
    metrics: gate_metrics.GateMetrics | None = None
    confirmed: list[dict[str, Any]] = field(default_factory=list)
    untestable: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    status_line: str = ""
    run_id: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.outcome is GateOutcome.APPROVED

    def funnel(self) -> dict[str, Any]:
        """Where claims went: candidates per lens -> confirmed / untestable / rejected.

        ``lens_calls`` is the parse state of every reviewer call, so a run that
        reported zero candidates is distinguishable from a run whose reviewers
        returned zero candidates.
        """
        by_lens: dict[str, int] = {}
        for c in self.candidates:
            lens = str(c.get("lens") or "?")
            by_lens[lens] = by_lens.get(lens, 0) + 1
        return {
            "candidates_by_lens": by_lens,
            "candidates": len(self.candidates),
            "confirmed": len(self.confirmed),
            "untestable": len(self.untestable),
            "rejected": len(self.rejected),
            "lens_calls": list(self.calls),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome.value,
            "sha": self.sha,
            "reasons": list(self.reasons),
            "candidates": list(self.candidates),
            "confirmed": list(self.confirmed),
            "untestable": list(self.untestable),
            "rejected": list(self.rejected),
            "funnel": self.funnel(),
            "metrics": self.metrics.to_dict() if self.metrics else {},
            "status_line": self.status_line,
        }


class GateCallRecorder:
    """Persists every agent call the gate makes, and summarises its parse state.

    A gate run that reports ``candidates=0`` is ambiguous: either the reviewers
    found nothing, or the findings were lost between the agent and the counter.
    This recorder makes that distinction checkable after the fact by writing one
    JSON file per call under ``<gate_dir>/calls/`` — the raw final text, the
    parsed object, the parse error, the exit code and the duration — and by
    keeping the per-call summary that ends up in :class:`GateResult` and
    :class:`~agent_fleet.gate.metrics.GateMetrics`.

    Records are appended for failures too: a dead or unparseable lens is exactly
    the case where the raw text is the only evidence of what happened.
    """

    def __init__(self, root: Path) -> None:
        self.dir = root / "calls"
        self.records: list[dict[str, Any]] = []
        self._n = 0

    def _next_name(self, stage: str) -> str:
        self._n += 1
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stage)
        return f"{safe}-{self._n}.json"

    def record(
        self,
        *,
        stage: str,
        model: str,
        raw: str,
        parsed: dict[str, Any] | None,
        parse_error: str,
        exit_code: int,
        duration_s: float,
        lens: str = "",
        n_items: int = 0,
        err: str = "",
    ) -> dict[str, Any]:
        """Write one call record and return its summary row."""
        entry: dict[str, Any] = {
            "stage": stage,
            "lens": lens,
            "model": model,
            "raw_len": len(raw or ""),
            "parsed_ok": parsed is not None,
            "n_items": n_items,
            "parse_error": (parse_error or err)[:400],
            "exit_code": int(exit_code),
            "duration_s": round(float(duration_s), 3),
        }
        self.records.append(entry)
        payload = {
            "stage": stage,
            "lens": lens,
            "model": model,
            "exit_code": entry["exit_code"],
            "duration_s": entry["duration_s"],
            "raw_len": entry["raw_len"],
            "parsed_ok": entry["parsed_ok"],
            "parse_error": entry["parse_error"],
            "n_items": n_items,
            "raw": raw or "",
            "parsed": parsed,
        }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / self._next_name(stage)).write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
        except (OSError, TypeError) as exc:
            # Persisting the trace must never change the verdict.
            logger.warning("gate could not persist %s call record: %s", stage, exc)
        return entry

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.records]


def status_line_for(outcome: GateOutcome, sha: str, reasons: list[str]) -> str:
    """The single line our automerge reads: ``HH:MM:SS PREMERGE-APPROVED <sha9>``.

    The timestamp is local time, matching the lane status files the rest of the
    fb tooling writes. On escalation the first reason is carried inline so the
    automerge log says *why* without a second lookup.
    """
    stamp = datetime.now().strftime("%H:%M:%S")
    if outcome is GateOutcome.APPROVED:
        return f"{stamp} PREMERGE-APPROVED {sha[:9]}"
    reason = reasons[0] if reasons else "unspecified"
    # The marker is replaced rather than merely rejected: a NEEDS-ESCALATION
    # line must never contain it, since the automerge's contract is that a line
    # is an approval when the marker stands alone ahead of a sha, and a reason
    # that quotes the marker verbatim would put a line in front of a reader who
    # cannot tell an approval from a complaint about one.
    reason = reason.replace(APPROVAL_MARKER, "the premerge-approved marker")
    return f"{stamp} NEEDS-ESCALATION {reason}"


def _write_status_line(path: Path, line: str) -> None:
    """Append the status line. Never raises — the run's verdict is already known."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        logger.warning("gate could not write status line to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Test orchestration
# ---------------------------------------------------------------------------


@dataclass
class TestRun:
    """The result of running the gate's whole deterministic test set at one head.

    ``tests_failed`` records that pytest exited 1 (tests failed) rather than 0.
    Verify needs that distinction and not just the count: a verifier's test that
    exits 0 is evidence the claim is *not* a blocker, however few ids it parsed.
    """

    __test__ = False  # a result record, not a pytest test class

    failing: list[str] = field(default_factory=list)
    infra_error: str = ""
    ran: int = 0
    tests_failed: bool = False

    @property
    def failing_set(self) -> set[str]:
        return set(self.failing)

    @property
    def count(self) -> int:
        return len(self.failing)


class GateTestRunner:
    """Runs the gate's test set at a worktree, memory-capped and slot-bounded.

    Two constraints are enforced here rather than trusted to the agents: every
    pytest is memory-capped (a 36GB runaway is a real failure mode on this
    machine), and every pytest holds a slot from the smaller test pool, so a
    wide verifier fan-out cannot launch twenty memory-capped processes at once.
    """

    def __init__(
        self,
        *,
        root: Path,
        memory: str = "6G",
        timeout_s: int = 900,
        package_dir: str | None = None,
        pool: SlotPool | None = None,
        use_systemd: bool | None = None,
    ) -> None:
        self.root = root
        self.memory = memory
        self.timeout_s = timeout_s
        self.package_dir = package_dir
        self.pool = pool
        self.use_systemd = systemd_run_available() if use_systemd is None else use_systemd

    def packages_for(self, test_files: list[str]) -> list[TestPackage]:
        return find_test_packages(self.root, test_files, package_dir=self.package_dir)

    def pytest_hint(self, test_file: str) -> str:
        """The exact memory-capped command the agents are told to run."""
        package = self.packages_for([test_file])
        rel_dir = package[0].rel_dir if package else "."
        cmd = build_pytest_command(
            [to_package_path(rel_dir, test_file)],
            memory=self.memory,
            use_systemd=self.use_systemd,
            package_dir=self.root / rel_dir,
        )
        return f"(cd {self.root / rel_dir} && {' '.join(cmd)})"

    def test_dir_hint(self, test_file: str) -> str:
        package = self.packages_for([test_file])
        rel_dir = package[0].rel_dir if package else "."
        return f"{rel_dir}/tests" if rel_dir != "." else "tests"

    def run(self, test_files: list[str]) -> TestRun:
        """Run *test_files* grouped per owning package; collect failing node ids."""
        if not test_files:
            return TestRun()
        result = TestRun()
        for package in self.packages_for(test_files):
            outcome = self._run_package(package)
            result.ran += 1
            if outcome.infra_error:
                result.infra_error = (
                    f"pytest could not run in {package.rel_dir} "
                    f"(exit {outcome.returncode}): {outcome.summary or outcome.stderr[:160]}"
                )
                return result
            if outcome.tests_failed:
                result.tests_failed = True
            result.failing.extend(to_repo_node_id(package.rel_dir, i) for i in outcome.failed_ids)
        result.failing = sorted(set(result.failing))
        return result

    def _run_package(self, package: TestPackage) -> PytestResult:
        guard = (
            self.pool.slot(timeout_s=None) if self.pool is not None else contextlib.nullcontext()
        )
        with guard:
            return run_pytest(
                package.dir,
                package.local_tests,
                memory=self.memory,
                timeout_s=self.timeout_s,
                use_systemd=self.use_systemd,
            )


class GateTestArchive:
    """Keeps gate-written test files so a fixer push cannot drop them.

    The gate tests live only in the worktree it created; the fixer pushes
    product code to the PR branch, so those test files would disappear at the
    next round. The archive copies each one out and materialises it back into
    every subsequent worktree at its original repo-relative path.
    """

    def __init__(self, root: Path) -> None:
        self.dir = root / "tests"
        self.dir.mkdir(parents=True, exist_ok=True)

    def stored_tests(self) -> list[Path]:
        """Every gate-written test the archive still holds, in name order.

        The archive is the only place a verifier's test survives a fixer push,
        so a later run that wants the gate's own evidence has to be able to ask
        what is in here. A fresh pipeline's ``evidence.gate_tests`` is always
        empty — it only records what *this* run produced — so a recheck driven
        by a new process could never see the archived tests.
        """
        return sorted(p for p in self.dir.glob("test_gate_*.py") if p.is_file())

    def store(self, source: Path) -> Path | None:
        if not source.is_file():
            return None
        target = self.dir / source.name
        shutil.copy2(source, target)
        return target

    def materialise(self, worktree: Path, rel_paths: list[str]) -> list[str]:
        """Copy archived tests back into *worktree*; return those restored."""
        restored: list[str] = []
        for rel in rel_paths:
            dest = worktree / rel
            if dest.is_file():
                continue
            archived = self.dir / Path(rel).name
            if not archived.is_file():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archived, dest)
            restored.append(rel)
        return restored


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixerResult:
    """What one fixer round actually did, measured rather than believed.

    Two facts, both read from the state the fixer left behind: whether its
    worktree holds a commit that was not the PR's head, and whether the PR's head
    moved. Neither is the fixer's say-so, because a fixer that disputes every
    finding will say it fixed something.
    """

    #: A commit exists in the fixer worktree that was not in the PR's head.
    committed: bool
    #: The forge reports a head for the PR that is not the one the gate read.
    pushed: bool

    @property
    def changed_nothing(self) -> bool:
        """No commit, or a commit that never reached the PR's head."""
        return not self.committed or not self.pushed


@dataclass
class _Evidence:
    """The gate's accumulated blocker evidence, split by how it was established."""

    confirmed: list[dict[str, Any]] = field(default_factory=list)
    untestable: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    rejected_items: list[dict[str, Any]] = field(default_factory=list)
    gate_tests: list[str] = field(default_factory=list)

    def reject(self, finding: Any, reason: str) -> None:  # noqa: ANN401 - Finding
        """Count AND record a rejected claim, so a false negative can be traced."""
        self.rejected += 1
        self.rejected_items.append(
            {
                "id": getattr(finding, "id", ""),
                "lens": getattr(finding, "lens", "") or "",
                "claim": (getattr(finding, "claim", "") or "")[:300],
                "reason": reason[:300],
            }
        )

    def confirmed_test_files(self) -> list[str]:
        return sorted({str(c["test_file"]) for c in self.confirmed if c.get("test_file")})

    def to_payload(self) -> dict[str, Any]:
        """The JSON shape a stage marker stores.

        Only the parts a reuse decision depends on: what was confirmed, what
        could not be tested, and which gate-written test files the confirmed
        blockers need re-running. The rejection trace stays out — it describes
        the run that produced the evidence, not the evidence itself.
        """
        return {
            "confirmed": self.confirmed,
            "untestable": self.untestable,
            "gate_tests": self.gate_tests,
        }

    @classmethod
    def restore(cls, payload: dict[str, Any]) -> _Evidence:
        """Rebuild from :meth:`to_payload`, tolerating a partial or odd marker.

        Every field falls back to empty rather than raising: a marker that
        restored half an evidence set would be worse than none, so the
        malformed parts are dropped and the rest is used.
        """
        return cls(
            confirmed=[d for d in payload.get("confirmed", []) or [] if isinstance(d, dict)],
            untestable=[d for d in payload.get("untestable", []) or [] if isinstance(d, dict)],
            gate_tests=[str(p) for p in payload.get("gate_tests", []) or []],
        )

    def merge_restored(self, payload: dict[str, Any]) -> None:
        """Fold a marker's evidence into what this run has already established.

        Step 0 runs before the reuse branch is even consulted, and a failing PR
        test it finds is already a confirmed blocker on *this* head. Replacing
        the evidence object would throw those away and let a PR whose own test
        is red be approved on the marker's word, so what a marker carries is
        added to what this run proved — never swapped for it.

        Entries are keyed by source and test id, because the same blocker can
        legitimately be confirmed twice (a re-run still finds it failing) while
        a distinct claim that happens to share an id is still worth keeping.
        """
        restored = _Evidence.restore(payload)
        known = {self._entry_key(item) for item in self.confirmed}
        for item in restored.confirmed:
            if self._entry_key(item) not in known:
                self.confirmed.append(item)
        self.untestable.extend(item for item in restored.untestable if item not in self.untestable)
        self.gate_tests.extend(path for path in restored.gate_tests if path not in self.gate_tests)

    @staticmethod
    def _entry_key(item: dict[str, Any]) -> tuple[str, str]:
        return str(item.get("source", "")), str(item.get("test_id") or item.get("id") or "")


class GatePipeline:
    """Runs the gate against one open PR. Owns worktrees, agents, and evidence."""

    def __init__(
        self,
        *,
        repo: Path,
        pr_number: int,
        config: GateConfig,
        policy: ModelPolicy,
        backend: LLMBackend,
        judge_backend: LLMBackend | None = None,
        gate_dir: Path,
        task_file: Path | None = None,
        status_file: Path | None = None,
        run_id: str | None = None,
        agent_pool: SlotPool | None = None,
        test_pool: SlotPool | None = None,
        use_systemd: bool | None = None,
        lane_slug: str | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.pr_number = pr_number
        self.config = config
        self.policy = policy
        self.backend = backend
        self.judge_backend = judge_backend
        self.gate_dir = gate_dir
        self.task_file = task_file
        self.status_file = status_file
        self.run_id = run_id or f"gate-{pr_number}-{uuid.uuid4().hex[:8]}"
        self.agent_pool = agent_pool
        self.test_pool = test_pool
        self.use_systemd = systemd_run_available() if use_systemd is None else use_systemd
        # An explicit slug wins; otherwise the config's; otherwise the PR's own
        # head ref, which is what makes the name unique per PR.
        self.lane_slug = lane_slug or config.lane_slug
        self.evidence = _Evidence()
        self.archive = GateTestArchive(gate_dir)
        self.recorder = GateCallRecorder(gate_dir)
        self.state = GateRunState(gate_dir)
        self._candidates: list[dict[str, Any]] = []
        self._runner: GateTestRunner | None = None
        #: step0's result, kept for the tier-0 decision. ``None`` means no
        #: changed test was run at all, which is a green run of nothing.
        self._step0_run: TestRun | None = None
        #: True when this run's evidence came from a marker rather than from
        #: agents dispatched here. Reuse has to re-run every confirmed test at
        #: the new head; a full run has already done that once this head.
        self._reused: bool = False
        #: The patch-id of the PR's own change at the head under review. Stored
        #: with the verify marker so a later head with the same change can find
        #: this run's evidence.
        self._pr_patch: str = ""

    # -- helpers ---------------------------------------------------------

    def _log(self, event: str, **data: object) -> None:
        logger.info("gate %s %s", event, data if data else "")
        run_log = _bound_run_log()
        if run_log is not None:
            run_log.emit(event, data=data)

    def _runner_for(self, root: Path) -> GateTestRunner:
        return GateTestRunner(
            root=root,
            memory=self.config.test_memory,
            timeout_s=self.config.test_timeout_s,
            package_dir=self.config.package_dir,
            pool=self.test_pool,
            use_systemd=self.use_systemd,
        )

    def _task_text(self) -> str:
        if self.task_file is None or not self.task_file.is_file():
            return _NO_TASK_TEXT
        return self.task_file.read_text(encoding="utf-8", errors="replace")[:20000]

    def _head_sha(self, worktree: Path) -> str:
        """The commit the gate is currently looking at (agents need it in prompts)."""
        return worktree_head_sha(worktree)

    def _model_for(self, *, backend_name: str, model: str | None, role: str) -> str:
        """Resolve the model for one role, failing fast on a policy violation."""
        return self.policy.check(backend=backend_name, model=model, role=role)

    def _call(
        self,
        *,
        backend: LLMBackend,
        prompt: str,
        model: str,
        cwd: Path,
        timeout_s: int,
        validate: Any,  # noqa: ANN401
        mode: AgentMode = "agent",
        list_key: str | None = None,
        **_: Any,  # noqa: ANN401 - `lens` is only used by _call_required's recorder
    ) -> Any:  # noqa: ANN401 - StructuredAnswer
        """One structured agent call. Defaults to AGENT mode (tools on).

        Lenses must run ``git diff`` / grep the repo / run tests, and verifiers must
        WRITE and run a failing test; in plan mode they can do neither, so lenses
        review shallowly and every claim is "rejected" — a false-negative gate
        (A/B on lake #3541: plan-mode fleet gate approved a PR the tool-enabled
        bash gate proved had 3 real blockers). Prompts keep them read-only and
        forbid pattern kills; gate worktrees are disposable."""
        return call_structured(
            backend,
            prompt,
            model=model,
            cwd=cwd,
            timeout_s=timeout_s,
            validate=validate,
            mode=mode,
            slot=self.agent_pool,
            list_key=list_key,
        )

    def _call_required(
        self,
        *,
        role: str,
        subject: str,
        invalid_ok: bool = False,
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Call an agent whose answer the verdict depends on; fail CLOSED.

        ``call_structured`` already retries once. A *dead* agent (killed,
        crashed, empty output) is never evidence of a clean PR, so it raises
        :class:`GateInfraError` and the gate escalates instead of approving on
        "no findings". An *invalid* answer raises too unless ``invalid_ok``
        (a verifier that answered without proof leaves the claim unproven, so
        rejecting it is correct); the caller then gets ``None``.

        Both outcomes are persisted to ``<gate_dir>/calls/`` before returning or
        raising, so a lost finding can always be traced to the answer that
        carried it.
        """
        try:
            answer = self._call(**kwargs)
        except StructuredCallError as exc:
            self.recorder.record(
                stage=role,
                model=str(kwargs.get("model", "")),
                raw=getattr(exc, "raw", ""),
                parsed=None,
                parse_error=str(exc),
                exit_code=getattr(exc, "exit_code", 1),
                duration_s=getattr(exc, "duration_s", 0.0),
                lens=str(kwargs.get("lens", "")),
            )
            self._log(f"gate.{role}.failed", subject=subject, kind=exc.kind, error=str(exc)[:200])
            if exc.kind == "invalid" and invalid_ok:
                return None
            if exc.kind == "timeout":
                # Out of budget is a dead agent, not an answer. Naming the stage
                # and how long it actually ran is the difference between an
                # escalation an operator can act on and one they have to guess at.
                budget = int(kwargs.get("timeout_s") or 0)
                raise GateInfraError(
                    f"fail-closed: {role} stage for {subject} timed out after "
                    f"{getattr(exc, 'duration_s', 0.0):.0f}s "
                    f"(stage budget {budget}s); no verdict was produced"
                ) from exc
            raise GateInfraError(
                f"fail-closed: {role} agent for {subject} gave no usable result "
                f"({exc.kind}): {str(exc)[:160]}"
            ) from exc
        self.recorder.record(
            stage=role,
            model=str(kwargs.get("model", "")),
            raw=answer.raw,
            parsed=answer.data,
            parse_error="",
            exit_code=0,
            duration_s=getattr(answer, "duration_s", 0.0),
            lens=str(kwargs.get("lens", "")),
            n_items=_n_items(answer.data),
        )
        return answer

    # -- step 0 ----------------------------------------------------------

    def run_pr_tests(self, worktree: Path) -> list[str]:
        """Run the PR's own changed tests at head; record their failures as blockers.

        A pytest exit >= 2 raises :class:`GateInfraError`: the suite could not
        run, so the gate has no evidence the PR is green. Treating that as a pass
        would approve a PR whose own tests were never executed.

        The result is also kept on :attr:`_step0_run`, because tier 0 approves on
        exactly this evidence: the PR's own changed tests green at head, with no
        model involved.
        """
        pr_tests = changed_test_files(worktree, self.config.base_branch)
        if not pr_tests:
            return []
        runner = self._runner_for(worktree)
        run = runner.run(pr_tests)
        self._step0_run = run
        if run.infra_error:
            raise GateInfraError(f"step0 {run.infra_error}")
        for node_id in run.failing:
            self.evidence.confirmed.append(
                {
                    "id": f"T-{node_id.split('::')[-1][:40]}",
                    "source": "pr-tests",
                    "claim": f"PR test fails at head: {node_id}",
                    "test_id": node_id,
                    "test_file": _file_of_node_id(node_id, pr_tests),
                    "lens": "pr-tests",
                }
            )
        self._log(
            "gate.step0",
            tests=len(pr_tests),
            failing=run.count,
            packages=run.ran,
        )
        return pr_tests

    # -- review tiering ---------------------------------------------------

    def review_tier(self, worktree: Path) -> ReviewTier:
        """How much review this PR earns, from the shape of its diff alone.

        Tiers 1 and 4 are the only ones that differ in cost, so this returns the
        lens set rather than a bare number. Tier 0 is decided in :meth:`run`,
        where step0's result is already known — a docs/tests-only PR whose own
        tests failed has blockers, whatever its diff looks like.
        """
        lines = diff_line_stats(worktree, self.config.base_branch)
        risky = prodsensitive_paths(worktree, self.config.base_branch, self.config)
        if lines > self.config.big_lines or risky:
            lenses = self.config.lenses[: self.config.max_parallel_lenses]
            return ReviewTier(tier=len(lenses), lenses=lenses, lines=lines, risky=risky)
        return ReviewTier(
            tier=1,
            lenses=(ALL_FOCUS_LENS,),
            lines=lines,
            note=f"non-test diff {lines} lines under big_lines={self.config.big_lines}",
        )

    def _lens_focus(self, lens: str) -> str:
        """Reviewer focus text for *lens*, including the synthetic all-focus lens."""
        return ALL_FOCUS if lens == ALL_FOCUS_LENS else self.config.focus_for(lens)

    def tier0_eligible(self, worktree: Path) -> list[str]:
        """The changed paths when this PR can be approved on evidence alone, else ``[]``.

        A PR whose changed files are *only* docs, tests and fixtures carries no
        product behaviour for a reviewer to reason about — the tests are the
        change. When they are green at head, approving is a statement about
        evidence, not a shortcut past it, so no model is dispatched.

        Returning the path list (empty meaning "not eligible") rather than a bool
        is what lets the caller log which files justified the tier without
        re-running the same git call.

        Every precondition is a refusal, and they all matter:

        - ``tier0: false`` turns the tier off for a repo that wants it off.
        - a step0 that could not run raised before reaching here, but the check
          is kept so this method is safe to call on its own.
        - **any** confirmed blocker — including a failing PR test — refuses.
          Approving a red PR on the grounds that its tests are the only thing it
          changes is exactly backwards.
        - an empty changed-file list refuses, so a git failure can never read as
          "docs/tests only, therefore approved".
        - an :meth:`tier0_evidence_gap` refuses, because a PR that deleted the
          tests it touched, or that only rewrote the suite config, has no step0
          run for the approval to rest on.
        """
        if not self.config.tier0:
            return []
        changed = changed_paths(worktree, self.config.base_branch)
        if not changed or any(not is_docs_or_test(path) for path in changed):
            return []
        if self._step0_run is not None and self._step0_run.infra_error:
            return []
        if self.tier0_evidence_gap(worktree):
            return []
        return [] if self.evidence.confirmed else changed

    def tier0_evidence_gap(self, worktree: Path) -> list[str]:
        """Why tier 0's own evidence never ran, or ``[]`` when it did.

        Tier 0 approves on one thing: the PR's own changed tests, green at head.
        Two shapes of test-only PR have no such run, and both are approved for
        that reason alone rather than in spite of it.

        - a **deletion** — :func:`changed_test_files` keeps only tests that still
          exist, so the one the PR removed drops out of the step0 set, the run
          never happens, and the empty result is indistinguishable from a green
          one. Removing the regression test that guards a data-loss bug then
          merges with a ``PREMERGE-APPROVED`` line and no test ever executed,
          while an edit to that same test is still sent to a model reviewer.
        - **suite-level test config** — ``conftest.py`` and the rest are not
          ``test_*.py``, so they are never step0-runnable, and they are what
          decides what the suite collects. A skip, an xfail or a silenced
          collection error is invisible to a run that never touched them.

        Nothing else sizes either one as risky either: the non-test line count
        excludes test paths, and no path matches a production-sensitive pattern.
        So the gap has to be read off the diff itself. The guard is a refusal
        *and* an escalation — see :meth:`run`, where a refusal alone would hand
        the PR to a reviewer that, finding nothing, approves it on the very
        evidence that never existed.
        """
        reasons: list[str] = []
        deleted = deleted_test_paths(worktree, self.config.base_branch)
        if deleted:
            reasons.append(
                f"the PR deletes {len(deleted)} of its own test file(s) "
                f"({', '.join(deleted[:3])}), so step0 ran none of them: "
                "removing a test removes the evidence that approves it"
            )
        config_paths = changed_test_config_paths(worktree, self.config.base_branch)
        if config_paths:
            reasons.append(
                f"the PR changes suite-level test config ({', '.join(config_paths[:3])}), "
                "which is never a step0 test: what the suite collects is not evidence"
            )
        return reasons

    @staticmethod
    def tier0_tier(changed: list[str]) -> ReviewTier:
        """The tier-0 record for an already-decided docs/tests-only diff.

        Takes the changed paths rather than re-reading the diff: :meth:`run` has
        them, and running the same git call twice to format one log line is the
        kind of duplication that goes stale.
        """
        return ReviewTier(
            tier=0,
            lenses=(),
            lines=0,
            note=f"{len(changed)} changed file(s), all docs/tests/fixtures; no model review",
        )

    # -- step 1 ----------------------------------------------------------

    def find(
        self,
        worktree: Path,
        ref: PullRequestRef,
        lenses: tuple[str, ...] | None = None,
    ) -> list[Finding]:
        """Run the lens reviewers in parallel and dedupe their candidate claims.

        *lenses* is the tier's reviewer set. It defaults to the configured lenses
        so a caller reviewing in isolation still gets the full set; :meth:`run`
        always passes the tier's. An explicitly empty tuple means *no reviewers*
        rather than falling back to the default, which is what makes "dispatch
        nothing" expressible.
        """
        model = self._model_for(
            backend_name=self.config.backend, model=self.config.model, role=ROLE_LENS
        )
        task_text = self._task_text()
        chosen = (self.config.lenses if lenses is None else lenses)[
            : self.config.max_parallel_lenses
        ]

        def _one(lens: str) -> list[Finding]:
            prompt = find_prompt(
                lens=lens,
                focus=self._lens_focus(lens),
                worktree=str(worktree),
                base_branch=resolve_diff_base(worktree, self.config.base_branch),
                head_sha=ref.short_sha,
                pr_number=self.pr_number,
                task_text=task_text,
            )
            answer = self._call_required(
                role="lens",
                subject=lens,
                lens=lens,
                backend=self.backend,
                prompt=prompt,
                model=model,
                cwd=worktree,
                timeout_s=self.config.stage_timeout(ROLE_LENS),
                validate=validate_findings,
                list_key="findings",
            )
            return _tag_lens(FindingsReport.from_dict(answer.data).findings, lens)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(chosen), 1)) as pool:
            batches = list(pool.map(_one, chosen))

        return _dedupe_findings([f for batch in batches for f in batch])[
            : self.config.max_candidates
        ]

    # -- step 2 ----------------------------------------------------------

    def verify(self, worktree: Path, findings: list[Finding], *, source: str) -> None:
        """Verify each testable claim with one failing test, run by the pipeline."""
        for finding in findings:
            if not finding.testable:
                # Never drop a claim the lens could not frame as a test: the judge rules on it.
                self.evidence.untestable.append(finding.to_dict())
                self._log("gate.verify.untestable", finding=finding.id, reason="lens-marked")
        testable = [f for f in findings if f.testable]
        if not testable:
            return
        model = self._model_for(
            backend_name=self.config.backend, model=self.config.model, role=ROLE_VERIFIER
        )
        runner = self._runner_for(worktree)

        def _one(finding: Finding) -> None:
            self._verify_one(finding, worktree=worktree, runner=runner, model=model, source=source)

        workers = max(1, self.config.max_parallel_verifiers)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_one, testable))

    def _verify_one(
        self,
        finding: Finding,
        *,
        worktree: Path,
        runner: GateTestRunner,
        model: str,
        source: str,
    ) -> None:
        test_name = gate_test_name(self.lane_slug, finding.id)
        prompt = verify_prompt(
            finding=finding,
            worktree=str(worktree),
            base_branch=resolve_diff_base(worktree, self.config.base_branch),
            head_sha=(self._head_sha(worktree) or "")[:9],
            pr_number=self.pr_number,
            test_dir_hint=runner.test_dir_hint(finding.file or "x"),
            pytest_cmd_hint=runner.pytest_hint(test_name),
            test_file_name=test_name,
        )
        answer = self._call_required(
            role="verify",
            subject=finding.id,
            invalid_ok=True,
            lens=finding.lens,
            backend=self.backend,
            prompt=prompt,
            model=model,
            cwd=worktree,
            timeout_s=self.config.stage_timeout(ROLE_VERIFIER),
            validate=validate_verify,
        )
        if answer is None:
            self.evidence.reject(finding, "verifier answer invalid (no proof)")
            return
        report = VerifyReport.from_dict(answer.data)
        if report.verdict is VerifyVerdict.UNTESTABLE:
            self.evidence.untestable.append(finding.to_dict())
            self._log("gate.verify.untestable", finding=finding.id)
            return
        if report.verdict is not VerifyVerdict.CONFIRMED or not report.test_file:
            self.evidence.reject(finding, f"verifier: {report.verdict.value}: {report.reason}")
            self._log("gate.verify.rejected", finding=finding.id, reason=report.reason[:160])
            return
        rel = _normalise_repo_path(report.test_file)
        test_path = worktree / rel
        if not test_path.is_file():
            self.evidence.reject(finding, "verifier named a test file that does not exist")
            self._log("gate.verify.discarded", finding=finding.id, reason="no test file")
            return
        run = runner.run([rel])
        if run.infra_error:
            self.evidence.reject(finding, f"verifier test could not run: {run.infra_error}")
            self._log("gate.verify.discarded", finding=finding.id, reason=run.infra_error[:160])
            _unlink(test_path)
            return
        if not run.tests_failed:
            # The verifier claimed CONFIRMED but its test does not fail on a test
            # assertion. The pipeline believes the test, not the verdict.
            self.evidence.reject(finding, f"verifier test did not fail (failing={run.count})")
            self._log(
                "gate.verify.discarded",
                finding=finding.id,
                reason=f"verifier test did not fail (failing={run.count})",
            )
            _unlink(test_path)
            return
        self.archive.store(test_path)
        self.evidence.confirmed.append(
            {**finding.to_dict(), "source": source, "test_file": rel, "reason": report.reason}
        )
        self.evidence.gate_tests.append(rel)
        self._log("gate.verify.confirmed", finding=finding.id, test_file=rel)

    # -- step 3 ----------------------------------------------------------

    def judge(self, worktree: Path, ref: PullRequestRef) -> None:
        """One judge call: rule on untestable claims and do its own blocker pass."""
        if not self.config.enable_judge or self.judge_backend is None:
            return
        model = self._model_for(
            backend_name=self.config.judge_backend, model=self.config.judge_model, role=ROLE_JUDGE
        )
        prompt = judge_prompt(
            worktree=str(worktree),
            base_branch=resolve_diff_base(worktree, self.config.base_branch),
            head_sha=ref.short_sha,
            pr_number=self.pr_number,
            confirmed=_json_blob(self.evidence.confirmed, 6000),
            untestable=_json_blob(self.evidence.untestable, 6000),
            task_text=self._task_text()[:8000],
        )
        answer = self._call_required(
            role="judge",
            subject="judge",
            backend=self.judge_backend,
            prompt=prompt,
            model=model,
            cwd=worktree,
            timeout_s=self.config.stage_timeout(ROLE_JUDGE),
            validate=validate_judge,
        )
        report = JudgeReport.from_dict(answer.data)
        for ruling in report.confirmed_untestable:
            self.evidence.confirmed.append(
                {
                    "id": str(ruling.get("id", "")),
                    "source": "judge-untestable",
                    "claim": str(ruling.get("reason", "")),
                    "test_file": None,
                }
            )
        new_blockers = _tag_lens(report.new_blockers, "judge")
        self._log(
            "gate.judge",
            untestable_rulings=len(report.untestable_rulings),
            untestable_real=len(report.confirmed_untestable),
            new_blockers=len(new_blockers),
        )
        if new_blockers:
            # The judge's own claims get no free pass: they go through verify.
            self.verify(worktree, new_blockers[: self.config.max_candidates], source="judge")

    def recheck_untestable(self, worktree: Path, start_sha: str, head_sha: str) -> bool:
        """One judge recheck: are the untestable blockers resolved at the new head?

        Returns True only when a judge actually ruled that nothing is left
        unresolved. A missing judge returns False, not True: this function is
        the only thing that can clear an untestable blocker, and "nobody was
        available to look" is not "the blocker is gone". Returning True there
        let a repo running with the judge disabled approve a defect the gate
        itself had ruled real, on a green suite that never demonstrated it.
        """
        if not self.config.enable_judge or self.judge_backend is None:
            return False
        untestable = [c for c in self.evidence.confirmed if c.get("source") == "judge-untestable"]
        if not untestable:
            return True
        model = self._model_for(
            backend_name=self.config.judge_backend, model=self.config.judge_model, role=ROLE_JUDGE
        )
        prompt = recheck_prompt(
            worktree=str(worktree),
            head_sha=head_sha[:9],
            pr_number=self.pr_number,
            start_sha=start_sha[:9],
            untestable=_json_blob(untestable, 6000),
        )
        try:
            answer = self._call(
                backend=self.judge_backend,
                prompt=prompt,
                model=model,
                cwd=worktree,
                timeout_s=self.config.stage_timeout(ROLE_JUDGE),
                validate=validate_recheck,
            )
        except StructuredCallError as exc:
            # A failed recheck is not a pass: we could not confirm resolution.
            self._log("gate.recheck.failed", error=str(exc)[:200])
            self.recorder.record(
                stage="recheck",
                model=model,
                raw=getattr(exc, "raw", ""),
                parsed=None,
                parse_error=str(exc),
                exit_code=getattr(exc, "exit_code", 1),
                duration_s=getattr(exc, "duration_s", 0.0),
            )
            return False
        self.recorder.record(
            stage="recheck",
            model=model,
            raw=answer.raw,
            parsed=answer.data,
            parse_error="",
            exit_code=0,
            duration_s=getattr(answer, "duration_s", 0.0),
            n_items=_n_items(answer.data),
        )
        report = RecheckReport.from_dict(answer.data)
        self._log("gate.recheck", unresolved=len(report.unresolved))
        return not report.unresolved

    # -- step 4 ----------------------------------------------------------

    def converge(
        self,
        *,
        ref: PullRequestRef,
        pr_tests: list[str],
    ) -> tuple[str, gate_metrics.GateMetrics]:
        """Run fix rounds while the failing set strictly shrinks. Returns (head, metrics)."""
        start_sha = ref.head_sha
        current = start_sha
        metric = gate_metrics.RoundMetric(round=0, head=current[:9], failing=0)

        if not self.config.enable_fix:
            return current, self._metrics(metric, ref, outcome=gate_metrics.OUTCOME_CAP)

        all_tests = sorted({*pr_tests, *self.evidence.gate_tests})
        test_wt = self.gate_dir / "recheck"
        prepare_worktree(self.repo, test_wt, current)
        try:
            self.archive.materialise(test_wt, self.evidence.gate_tests)
            run = self._runner_for(test_wt).run(all_tests)
            if run.infra_error:
                remove_worktree(self.repo, test_wt)
                raise GateInfraError(f"converge {run.infra_error}")
            metric.failing = run.count
            previous = run.failing_set
        finally:
            remove_worktree(self.repo, test_wt)

        rounds = [metric]
        untestable_open = [
            c for c in self.evidence.confirmed if c.get("source") == "judge-untestable"
        ]
        if run.count == 0 and not untestable_open:
            return current, self._metrics(metric, ref, outcome=gate_metrics.OUTCOME_CONVERGED)

        # Nothing fails and the only blockers are untestable ones no local test
        # can demonstrate. The convergence rule below is defined on a shrinking
        # failing set, so there is no set to shrink here — but "no failing test"
        # is not "no work": the judge ruled these real, and a docs
        # contradiction or a script's behaviour is fixed by editing the thing.
        # So dispatch exactly one fix round carrying the untestable list, then
        # let the recheck judge decide. One round, not a loop: with no test to go
        # green there is no measurable progress, only the judge's yes/no.
        untestable_only = run.count == 0 and bool(untestable_open)
        # The push target is the PR's own ``headRefName`` and nothing else. A
        # lane-derived branch here — ``fb/<lane>``, which is what a config
        # override would supply — moves a head nobody re-gates, and the run
        # then reports "no push" over a PR whose head did move. That is the
        # whole of the documents-1d dq1d failure, and _standard_fixer below
        # already refuses the same override. A fixer writes into the PR; the
        # lane is how the work was routed, not where it lands.
        push_branch = ref.head_ref
        model = self._model_for(
            backend_name=self.config.backend, model=self.config.model, role=ROLE_FIX
        )
        outcome = gate_metrics.OUTCOME_CAP
        round_limit = 1 if untestable_only else max(1, self.config.max_fix_rounds)

        for round_number in range(1, round_limit + 1):
            fix_wt = self.gate_dir / f"fix{round_number}"
            prepare_worktree(self.repo, fix_wt, current)
            try:
                self.archive.materialise(fix_wt, self.evidence.gate_tests)
                prompt = fix_prompt(
                    pr_number=self.pr_number,
                    worktree=str(fix_wt),
                    head_sha=current[:9],
                    push_branch=push_branch,
                    round_number=round_number,
                    failing="\n".join(sorted(previous)),
                    confirmed=_json_blob(self.evidence.confirmed, 8000),
                    untestable=_json_blob(untestable_open, 4000),
                    all_tests=" ".join(all_tests),
                    pytest_cmd_hint=self._runner_for(fix_wt).pytest_hint(
                        gate_test_name(self.lane_slug, "x")
                    ),
                    task_text=self._task_text()[:6000],
                )
                self._run_fixer(prompt, model=model, cwd=fix_wt)
            finally:
                remove_worktree(self.repo, fix_wt)

            fetch_base(self.repo, self.config.base_branch)
            new_head = current_pr_head(self.repo, self.pr_number)
            if new_head == current:
                outcome = (
                    gate_metrics.OUTCOME_UNTESTABLE_NEEDS_REVIEW
                    if untestable_only
                    else gate_metrics.OUTCOME_NO_PUSH
                )
                self._log("gate.round", round=round_number, head=new_head[:9], outcome=outcome)
                break

            check_wt = self.gate_dir / f"re{round_number}"
            prepare_worktree(self.repo, check_wt, new_head)
            try:
                self.archive.materialise(check_wt, self.evidence.gate_tests)
                run = self._runner_for(check_wt).run(all_tests)
                if run.infra_error:
                    rounds.append(
                        gate_metrics.RoundMetric(
                            round=round_number, head=new_head[:9], failing=len(previous)
                        )
                    )
                    outcome = gate_metrics.OUTCOME_TESTS_BROKEN
                    self._log(
                        "gate.round",
                        round=round_number,
                        head=new_head[:9],
                        outcome=outcome,
                        error=run.infra_error[:200],
                    )
                    current = new_head
                    break
                now = run.failing_set
            finally:
                remove_worktree(self.repo, check_wt)

            fixed = len(previous - now)
            new_failures = len(now - previous)
            rounds.append(
                gate_metrics.RoundMetric(
                    round=round_number,
                    head=new_head[:9],
                    failing=run.count,
                    fixed=fixed,
                    new_failures=new_failures,
                )
            )
            self._log(
                "gate.round",
                round=round_number,
                head=new_head[:9],
                failing_before=len(previous),
                failing_after=run.count,
                fixed=fixed,
                new_failures=new_failures,
            )
            current = new_head
            if untestable_only and run.count == 0:
                # No test existed to go green, so the failing-set numbers above
                # say nothing about this defect. The recheck judge below rules
                # on it; until then it is still open.
                outcome = gate_metrics.OUTCOME_UNTESTABLE_UNRESOLVED
                break
            if untestable_only:
                # The round set out to fix something no test can demonstrate, and
                # it broke a test on the way past. The recheck judge is only
                # ever shown the untestable claim, so it cannot see this, and
                # reading the round as merely "unresolved" would let the judge
                # rule the untestable blocker fixed and the run converge — an
                # approval over a head with a red test. The deterministic half
                # is authoritative at whatever head it ran on, and it is red.
                outcome = gate_metrics.OUTCOME_STALLED
                break
            if run.count == 0:
                # Every test is green. The deterministic half has converged; if
                # untestable blockers remain, the judge recheck below decides
                # them, so stop fixing and go straight there.
                outcome = (
                    gate_metrics.OUTCOME_CONVERGED
                    if not untestable_open
                    else gate_metrics.OUTCOME_UNTESTABLE_UNRESOLVED
                )
                break
            # Progress means the failing set strictly shrank with nothing new
            # broken. Anything else is a stall, whatever the round count.
            if new_failures > 0 or fixed == 0 or run.count >= len(previous):
                outcome = gate_metrics.OUTCOME_STALLED
                break
            previous = now

        head_wt = self.gate_dir / "final"
        prepare_worktree(self.repo, head_wt, current)
        try:
            needs_recheck = untestable_open and outcome in {
                gate_metrics.OUTCOME_CONVERGED,
                gate_metrics.OUTCOME_UNTESTABLE_UNRESOLVED,
            }
            if needs_recheck and self.recheck_untestable(head_wt, start_sha, current):
                outcome = gate_metrics.OUTCOME_CONVERGED
            elif needs_recheck:
                outcome = (
                    # The untestable round was the only shot at a blocker no
                    # test can demonstrate. It is still open, and repeating it
                    # would spend fixer budget on a decision only a judge can
                    # make: name what a human has to look at.
                    gate_metrics.OUTCOME_UNTESTABLE_NEEDS_REVIEW
                    if untestable_only
                    else gate_metrics.OUTCOME_UNTESTABLE_UNRESOLVED
                )
        finally:
            remove_worktree(self.repo, head_wt)

        return current, self._metrics(metric, ref, outcome=outcome, rounds=rounds, head=current)

    def _run_fixer(self, prompt: str, *, model: str, cwd: Path) -> None:
        """One fix round. Free-form output (it commits and pushes), so no schema.

        A fixer that runs out of budget is a dead stage, not a failed attempt:
        without this it was logged and the round carried on to find "no push",
        which reports the wrong cause and invites a retry of the same budget.
        """
        budget = self.config.stage_timeout(ROLE_FIX)
        guard = (
            self.agent_pool.slot(timeout_s=None)
            if self.agent_pool is not None
            else (contextlib.nullcontext())
        )
        with guard:
            result = self.backend.run(
                prompt,
                max_tokens=0,
                timeout_s=budget,
                cwd=cwd,
                model=model,
                mode="agent",
            )
        timed_out = result.exit_code == TIMEOUT_EXIT
        self.recorder.record(
            stage="fix",
            model=model,
            raw=result.stdout or "",
            parsed=None,
            parse_error=(
                f"fix stage timed out after {budget}s"
                if timed_out
                else ("" if result.exit_code == 0 else (result.stderr or "")[:400])
            ),
            exit_code=result.exit_code,
            duration_s=getattr(result, "duration_s", 0.0),
        )
        if timed_out:
            self._log("gate.fix.timeout", budget_s=budget)
            raise GateInfraError(
                f"fail-closed: fix stage timed out after "
                f"{getattr(result, 'duration_s', 0.0):.0f}s (stage budget {budget}s); "
                f"no verdict was produced"
            )
        if result.exit_code != 0:
            self._log("gate.fix.failed", error=(result.stderr or "")[:200])

    # -- approval carry-over ----------------------------------------------

    def recheck_carry_over(
        self,
        *,
        approved_sha: str,
        head_sha: str,
        status_file: Path,
        test_run: TestRun,
        test_files: list[str],
    ) -> GateResult:
        """Decide whether an approval survives a rebase onto a new head.

        Four conditions, all required, because an approval carried onto the
        wrong code is worse than no approval at all:

        1. there is an ``approved_sha`` and it is a real commit,
        2. the status file records a ``PREMERGE-APPROVED`` line for it,
        3. the change's patch-id is unchanged (the gate's own test directory
           excluded — those files are gate evidence, not the PR's change, and a
           collision on one is what routinely forced the rebase),
        4. every test is green on the new head, with a run that actually
           happened: an infra error is "we could not check", never a pass, and
           so is a run that never collected a single test.

        Any of them missing and the answer is a full gate. The approved sha is
        never the sha emitted: the status line names the *new* head, since that
        is the commit the automerge will actually take.

        Reasons never name the approval marker. A ``NEEDS-ESCALATION`` line
        quoting it is a verdict whose text happens to contain a marker, and the
        automerge must not — and cannot be allowed to — read it as an approval
        for a head the gate just refused.
        """
        reasons: list[str] = []
        approved = approved_sha.strip()
        if not approved:
            return self._carry_over_refusal(head_sha, "no approved sha given")
        if not has_approval_line(status_file, approved):
            return self._carry_over_refusal(
                head_sha, f"no premerge-approved status line for {approved[:9]}"
            )

        base = resolve_diff_base(self.repo, self.config.base_branch)
        old_patch = patch_id(self.repo, approved, base)
        new_patch = patch_id(self.repo, head_sha, base)
        if not new_patch:
            return self._carry_over_refusal(
                head_sha, f"head {head_sha[:9]} is not a known commit in this repo"
            )
        if not old_patch:
            return self._carry_over_refusal(
                head_sha, f"approved sha {approved[:9]} is not a known commit in this repo"
            )
        if old_patch != new_patch:
            return self._carry_over_refusal(head_sha, "change differs from the approved patch")

        if test_run.infra_error:
            return self._carry_over_refusal(
                head_sha, f"tests could not run on the rebased head: {test_run.infra_error[:120]}"
            )
        if not test_run.ran:
            # An empty test set gives a TestRun identical to a real green run
            # on every field this function reads, so "we ran nothing" was being
            # reported as "the tests passed" and a PREMERGE-APPROVED line was
            # written for a head nothing ever exercised. Zero verification is
            # not a pass.
            return self._carry_over_refusal(
                head_sha,
                f"no test ran on the rebased head ({len(test_files)} test file(s) "
                "in the set), so there is no green run to carry over",
            )
        if test_run.count:
            reasons.append(
                f"{test_run.count} test(s) fail on the rebased head: "
                f"{', '.join(test_run.failing[:3])}"
            )
            return self._carry_over_refusal(head_sha, reasons[0])

        reasons.append(
            f"approval carried over from {approved[:9]}: patch-identical "
            f"(patch-id {new_patch[:12]}, gate tests excluded) and all "
            f"{len(test_files)} test(s) green on the rebased head"
        )
        return self._carry_over_approval(head_sha, reasons)

    def _carry_over_refusal(self, head_sha: str, reason: str) -> GateResult:
        """NEEDS_ESCALATION with the full gate named as what is required."""
        message = f"full gate required: {reason}"
        self._log("gate.recheck.refused", head=head_sha[:9], reason=reason)
        return self._carry_over_result(GateOutcome.NEEDS_ESCALATION, "", [message])

    def _carry_over_approval(self, head_sha: str, reasons: list[str]) -> GateResult:
        self._log("gate.recheck.approved", head=head_sha[:9], reason=reasons[0][:160])
        return self._carry_over_result(GateOutcome.APPROVED, head_sha, reasons)

    def _carry_over_result(self, outcome: GateOutcome, sha: str, reasons: list[str]) -> GateResult:
        line = status_line_for(outcome, sha, reasons)
        if self.status_file is not None:
            _write_status_line(self.status_file, line)
        metric = gate_metrics.GateMetrics(
            run_id=self.run_id,
            repo=self.repo.name,
            pr=self.pr_number,
            start_sha=sha or self.run_id,
            head_sha=sha,
            outcome=(
                gate_metrics.OUTCOME_CONVERGED if outcome is GateOutcome.APPROVED else "recheck"
            ),
            reasons=list(reasons),
        )
        # A carried approval is not a gate run. Marking it keeps the metrics
        # table honest about how much review a given approval actually had.
        metric.calls = [{"stage": "recheck", "lens": "carried-over", "exit_code": 0}]
        metric.append_metrics()
        return GateResult(
            outcome=outcome,
            sha=sha,
            reasons=list(reasons),
            metrics=metric,
            status_line=line,
            run_id=self.run_id,
        )

    # -- metrics ---------------------------------------------------------

    def _metrics(
        self,
        base_round: gate_metrics.RoundMetric,
        ref: PullRequestRef,
        *,
        outcome: str,
        rounds: list[gate_metrics.RoundMetric] | None = None,
        head: str = "",
    ) -> gate_metrics.GateMetrics:
        all_rounds = rounds if rounds is not None else [base_round]
        untestable_real = sum(
            1 for c in self.evidence.confirmed if c.get("source") == "judge-untestable"
        )
        return gate_metrics.GateMetrics(
            run_id=self.run_id,
            repo=self.repo.name,
            pr=self.pr_number,
            start_sha=ref.head_sha,
            head_sha=head or ref.head_sha,
            outcome=outcome,
            candidates=len(self._candidates),
            confirmed=len(self.evidence.confirmed),
            rejected=self.evidence.rejected,
            untestable=len(self.evidence.untestable),
            untestable_real=untestable_real,
            rounds=all_rounds,
            calls=self.recorder.rows(),
        )

    # -- the STANDARD bar -------------------------------------------------

    def run_standard(
        self,
        ref: PullRequestRef,
        worktree: Path,
        pr_tests: list[str],
    ) -> GateResult:
        """Review a non-sensitive diff under the risk-matched bar.

        One reviewer covering all four focuses — no parallel lenses, no
        per-claim verifier, no judge. That is the only difference in *who looks*;
        the difference that matters is what happens next, and that is
        :mod:`agent_fleet.gate.standard`'s state machine, given here the two
        facts it reads: what the reviewer and step0 found, and how many passes
        this PR has already spent on earlier heads.

        Every terminal state except approval ends the run as NEEDS_ESCALATION,
        which is the automerge's "not approved" and exits non-zero. The label
        in the reason line is what distinguishes them after the fact — a cheap-bar
        approval, a re-gate, and a fall-back to the full gate are three
        different claims about a PR and must not read the same in the log.
        """
        reasons: list[str] = []
        # A bounded tail, not the whole file: metrics.jsonl is shared by every run
        # on the host and is never rotated, and this counter needs one PR's rows.
        passes = standard_prior_passes(
            gate_metrics.read_metrics(limit=STANDARD_HISTORY_ROWS),
            repo=self.repo.name,
            pr=self.pr_number,
            max_passes=self.config.standard_max_passes,
        )
        findings = self.find(worktree, ref, (ALL_FOCUS_LENS,))
        self._candidates = [f.to_dict() for f in findings]
        self._log(
            "gate.find",
            candidates=len(findings),
            lenses=[ALL_FOCUS_LENS],
            tier=STANDARD_TIER,
        )

        # Without a verifier or a judge there is nothing between the reviewer and
        # the verdict, so its findings are the blockers: promoting them is what
        # makes "reported blockers" mean the same thing here as in the full gate.
        for finding in findings:
            self.evidence.confirmed.append(
                {
                    **finding.to_dict(),
                    "source": "all-focus",
                    "test_file": None,
                }
            )
        pr_tests_failed = any(c.get("source") == "pr-tests" for c in self.evidence.confirmed)
        state = StandardState(
            tier=STANDARD_TIER,
            findings=len(self.evidence.confirmed),
            pr_tests_failed=pr_tests_failed,
            passes=passes,
            max_passes=self.config.standard_max_passes,
            head=ref.head_sha,
        )
        decision = standard_next_action(state)
        self._log("gate.standard", **decision.to_dict(), findings=state.findings)

        if decision.action is StandardAction.APPROVE:
            reasons.append(standard_approval_reason(state))
            return self._finish(
                GateOutcome.APPROVED,
                ref.head_sha,
                reasons,
                ref,
                tier=STANDARD_TIER,
                metric_outcome=STD_OUTCOME_APPROVED,
                passes=decision.passes,
            )

        if decision.action is StandardAction.FIX_AND_REGATE:
            result = self._standard_fixer(ref, pr_tests, passes)
            # What the fixer did is a fact only it can report, and the bar's rules
            # are written in terms of that fact — so the same state machine is
            # asked again with the answer in hand rather than the fallback being
            # hand-built here, which is what left a no-op run with no reason
            # recorded against it and its pass count claiming progress. The
            # action can only move to FALLBACK: nothing between here and the
            # re-gate changes what is blocked.
            after = standard_next_action(
                replace(state, fixer_changed_nothing=result.changed_nothing)
            )
            if after.action is StandardAction.FALLBACK:
                reason = (
                    after.fallback_reason.value
                    if after.fallback_reason
                    else FallbackReason.DISPUTED.value
                )
                reasons.append(
                    f"full evidence gate required: the standard bar gave up on this head "
                    f"({reason}) at pass {after.passes}; this head goes to the full review pipeline"
                )
                return self._finish(
                    GateOutcome.NEEDS_ESCALATION,
                    ref.head_sha,
                    reasons,
                    ref,
                    tier=STANDARD_TIER,
                    metric_outcome=STD_OUTCOME_FALLBACK,
                    passes=after.passes,
                )
            reasons.append(
                f"re-gate new head: one standard-bar fixer pass ({decision.passes} of "
                f"{state.max_passes}) dispatched; the pushed head is not trusted and is "
                "re-gated from scratch"
            )
            return self._finish(
                GateOutcome.NEEDS_ESCALATION,
                ref.head_sha,
                reasons,
                ref,
                tier=STANDARD_TIER,
                metric_outcome=STD_OUTCOME_FIX_AND_REGATE,
                passes=decision.passes,
            )

        reason = decision.fallback_reason.value if decision.fallback_reason else "unknown"
        reasons.append(
            f"full evidence gate required: the standard bar gave up on this head "
            f"({reason}) after {decision.passes} pass(es); this PR now gets the full "
            "review pipeline"
        )
        return self._finish(
            GateOutcome.NEEDS_ESCALATION,
            ref.head_sha,
            reasons,
            ref,
            tier=STANDARD_TIER,
            metric_outcome=STD_OUTCOME_FALLBACK,
            passes=decision.passes,
        )

    def _standard_fixer(
        self,
        ref: PullRequestRef,
        pr_tests: list[str],
        passes: int,
    ) -> FixerResult:
        """Dispatch the single fixer the STANDARD bar allows, and push its work.

        The fixer is told the reviewer's findings and the PR's failing tests and
        is asked for a focused test per real finding, then it commits and pushes
        to the PR's own head ref. The push target is ``ref.head_ref`` — the
        ``headRefName`` the forge reports — and never a name derived from the
        lane: a fixer that pushes to a lane-derived branch moves a head nobody is
        re-gating, and the run then reports "no push" over a PR that did move.

        What it did is measured, not believed: the worktree the fixer ran in is
        the one place its commits can be seen, so the head that existed before
        the run and the head after it are compared there. The forge is asked
        afterwards as a cross-check, and a push the worktree cannot confirm is
        treated as no push — the disputed case has to be the safe answer, since
        the alternative is paying for a re-gate of a head that never moved.
        """
        fix_wt = self.gate_dir / "standard-fix"
        prepare_worktree(self.repo, fix_wt, ref.head_sha)
        try:
            model = self._model_for(
                backend_name=self.config.backend, model=self.config.model, role=ROLE_FIX
            )
            failing = self._step0_run.failing if self._step0_run else []
            prompt = fix_prompt(
                pr_number=self.pr_number,
                worktree=str(fix_wt),
                head_sha=ref.head_sha[:9],
                push_branch=ref.head_ref,
                round_number=passes + 1,
                failing="\n".join(sorted(failing)),
                confirmed=_json_blob(self.evidence.confirmed, 8000),
                untestable="",
                all_tests=" ".join(pr_tests),
                pytest_cmd_hint=self._runner_for(fix_wt).pytest_hint(
                    gate_test_name(self.lane_slug, "x")
                ),
                task_text=self._task_text()[:6000],
            )
            self._run_fixer(prompt, model=model, cwd=fix_wt)
            return self._standard_fixer_result(fix_wt, ref)
        finally:
            remove_worktree(self.repo, fix_wt)

    def _standard_fixer_result(self, fix_wt: Path, ref: PullRequestRef) -> FixerResult:
        """Whether the fixer round left the PR's head with a new commit on it.

        Two independent witnesses have to agree before this bar treats a fixer as
        having fixed anything: the worktree it was given has to hold a commit
        that was not the head, and the forge has to report a head that moved. A
        commit the fixer never pushed fixes nothing anybody will re-gate, and a
        head that moved without one is not the fixer's work, so either witness
        missing reads as changed-nothing.
        """
        fixer_head = worktree_head_sha(fix_wt)
        committed = bool(fixer_head) and fixer_head != ref.head_sha
        fetch_base(self.repo, self.config.base_branch)
        pushed = current_pr_head(self.repo, self.pr_number) != ref.head_sha
        return FixerResult(committed=committed, pushed=pushed)

    # -- rebase and restart reuse ----------------------------------------

    def _pre_review_conflict(self, ref: PullRequestRef) -> str:
        """Stop before review if *base* does not merge into the PR's head.

        Returns a non-empty reason when the PR must go back to a rebase agent,
        and ``""`` when there is nothing to stop for. A conflict is a fact
        about the diff that costs milliseconds to establish, and discovering it
        after a full find→verify→judge run is the single largest avoidable
        expense in the gate — measured today at 3 of 10 merge-with-main
        failures being plain conflicts.

        Uses the PR's *own* base, not ``main``: a PR cut against a release
        branch legitimately conflicts with main, and refusing it would send
        every release PR to a rebase agent for ever.

        A git failure is deliberately **not** a conflict. ``merge_conflict_check``
        distinguishes them because a git too old for ``--write-tree`` would
        otherwise make every run escalate, which is worse than the waste this
        saves.
        """
        base = ref.base_ref or self.config.base_branch
        check = merge_conflict_check(self.repo, ref.head_sha, resolve_diff_base(self.repo, base))
        if check.conflict_files:
            files = ", ".join(check.conflict_files[:5])
            rest = len(check.conflict_files) - 5
            more = f" (+{rest} more)" if rest > 0 else ""
            self._log(
                "gate.merge.conflict",
                head=ref.short_sha,
                base=base,
                files=len(check.conflict_files),
            )
            return (
                f"merged-tree regression check failed at {ref.short_sha} "
                f"(pre-review: merge conflict with {base} in {files}{more})"
            )
        if check.git_error:
            self._log("gate.merge.check_error", head=ref.short_sha, base=base)
        return ""

    def _reusable_evidence(self, ref: PullRequestRef) -> StageState | None:
        """Verification evidence this head can stand on, or ``None``.

        Two ways to have it, and they differ only in the key:

        * a marker at *this* head — the gate was re-launched on the same
          commit, so the stage that died is the one that finished;
        * a marker at any head with the same ``patch-id`` — the PR was rebased
          and its own change is byte-identical, which is the common case,
          because what forces a rebase is usually the base moving under it.

        In both cases the tests behind the evidence are re-run at this head by
        :meth:`_reused_evidence_is_sound` before the evidence is used, so what
        is reused is the *findings*, never the verdict.
        """
        same_head = self.state.read(STAGE_VERIFY, ref.head_sha)
        if same_head is not None and same_head.reusable:
            return same_head
        if not self._pr_patch:
            self._pr_patch = patch_id(
                self.repo, ref.head_sha, resolve_diff_base(self.repo, self._pr_base(ref))
            )
        prior = self.state.reusable_verified(self._pr_patch)
        if prior is not None:
            # A patch-id is the *PR's own change*, and it says nothing about
            # what the base did. A rebase onto a moved main can introduce a
            # fresh regression in the very files the earlier run reviewed while
            # leaving the PR's diff — and so its patch-id — byte-identical.
            #
            # When the earlier run approved it holds no findings, and its
            # approval was the statement "these lenses found nothing in this
            # diff at that head". That statement is not a transferable
            # artifact: carrying it across a rebase lets a head no reviewer
            # ever looked at take the reuse branch, dispatch nothing, and land
            # on "nothing confirmed, therefore approved". An empty payload
            # therefore proves nothing, so the *soundness* re-run cannot rescue
            # it (it has no test to re-run) and reuse is refused outright.
            #
            # Same-head reuse is untouched: there the code is identical, so the
            # earlier "found nothing" is a statement about exactly this head.
            evidence = prior.payload.get("evidence", {})
            if not evidence.get("confirmed") and not evidence.get("gate_tests"):
                self._log(
                    "gate.reuse.refused",
                    prior_head=prior.head_sha[:9],
                    patch_id=self._pr_patch[:12],
                    reason="no findings to carry across a rebase",
                )
                return None
            self._log(
                "gate.reuse",
                pr_diff="unchanged",
                prior_head=prior.head_sha[:9],
                patch_id=self._pr_patch[:12],
            )
        return prior

    def _pr_base(self, ref: PullRequestRef) -> str:
        """The branch this PR's diff is read against: its own base if reported."""
        return ref.base_ref or self.config.base_branch

    def _reused_evidence_is_sound(self, worktree: Path, payload: dict[str, Any]) -> bool:
        """Do the reused confirmed tests still fail at this head?

        The safety condition on reuse, and the reason the verdict is never
        carried: the findings are reused, but every test that established them
        is run again here, on *this* head. A base merge can repair the bug a
        blocker described, in which case the blocker is stale and the evidence
        is dropped rather than acted on.

        The PR's own tests are step 0 and always run before this; the
        merged-with-base check runs before that.
        """
        gate_tests = [str(p) for p in payload.get("gate_tests", []) or []]
        confirmed = payload.get("confirmed", []) or []
        if not gate_tests or not confirmed:
            return True
        self.archive.materialise(worktree, gate_tests)
        run = self._runner_for(worktree).run(gate_tests)
        if run.infra_error:
            self._log("gate.reuse.no_run", reason=run.infra_error[:160])
            return False
        if not run.tests_failed:
            self._log("gate.reuse.stale", reason="no confirmed test fails at this head")
            return False
        return True

    # -- entry point -----------------------------------------------------

    def run(self) -> GateResult:
        """Execute the whole gate. Always returns a result; never raises."""
        reasons: list[str] = []
        ref: PullRequestRef | None = None
        worktree = self.gate_dir / "wt"
        outcome = GateOutcome.NEEDS_ESCALATION
        sha = ""
        converged_metric: gate_metrics.GateMetrics | None = None
        infra_failed = False
        # Whether *this* run got as far as verifying, and so owns the marker
        # that _settle_state is about to finish. A run that returns early —
        # unresolvable ref, pre-review conflict, tier 0, tier0 evidence gap —
        # never verified anything, and must not speak for a marker it did not
        # write. See _settle_state.
        verified_here = False
        try:
            fetch_base(self.repo, self.config.base_branch)
            ref = resolve_pull_request(self.repo, self.pr_number)
            if not ref.is_open:
                reasons.append(f"PR #{ref.number} is {ref.state}, not OPEN")
                return self._finish(outcome, "", reasons, ref)
            self._log("gate.start", pr=ref.number, head=ref.short_sha, branch=ref.head_ref)

            # Before anything is dispatched: can the base even merge into this
            # head? A conflict is the orchestrator's signal to send the PR to a
            # rebase agent, and it is far cheaper to establish here than after a
            # full review.
            conflict = self._pre_review_conflict(ref)
            if conflict:
                return self._finish(GateOutcome.NEEDS_ESCALATION, "", [conflict], ref)

            prepare_worktree(self.repo, worktree, ref.head_sha)
            self._runner = self._runner_for(worktree)
            pr_tests = self.run_pr_tests(worktree)

            # Tier 0: a docs/tests-only PR with its own tests green at head has
            # nothing for a model to review. Its approval rests on step0 plus the
            # recheck that runs before the merge, both of which already happened
            # or already refuse. Every other PR is reviewed, so the tiers below
            # only ever choose between one reviewer and the full lens set.
            tier0 = self.tier0_eligible(worktree)
            if tier0:
                self._log("gate.tier", **tier_fields(self.tier0_tier(tier0)))
                sha = ref.head_sha
                outcome = GateOutcome.APPROVED
                return self._finish(outcome, sha, reasons, ref)

            # Tier 0's evidence is a green run of the PR's own changed tests.
            # A PR that deletes them, or that only rewrites the suite config
            # that decides what runs, has no such run — so its approval would
            # rest on a green run of nothing. Refusing tier 0 is not enough
            # here: that hands the PR to a lens, and a lens with nothing to
            # report is exactly the "no blockers, therefore approved" verdict
            # this refuses. Review is what the tier was skipping, so the run
            # has to end as the escalation it is.
            evidence_gap = self.tier0_evidence_gap(worktree)
            if evidence_gap:
                self._log("gate.tier0.gap", reasons=evidence_gap)
                return self._finish(GateOutcome.NEEDS_ESCALATION, "", evidence_gap, ref)

            # Risk-matched bar. A diff that touches nothing sensitive is reviewed
            # once by an all-focus reviewer under the STANDARD rules (one fixer
            # pass, then re-gate); anything sensitive keeps today's full evidence
            # pipeline below, unchanged. Decided after step0 so a red PR test is
            # already a confirmed blocker the STANDARD state machine can act on.
            changed = changed_paths(worktree, self.config.base_branch)
            if standard_select_tier(self.config, changed) == STANDARD_TIER:
                return self.run_standard(ref, worktree, pr_tests)

            tier = self.review_tier(worktree)
            self._log("gate.tier", **tier_fields(tier))

            # Restart or rebase: stand on the last run's evidence instead of
            # re-buying it. Only reached for a PR that already needs the full
            # pipeline — tier 0 and STANDARD have their own shorter paths.
            prior = self._reusable_evidence(ref)
            if prior is not None and not self._reused_evidence_is_sound(
                worktree, prior.payload.get("evidence", {})
            ):
                self._log("gate.reuse.abandoned", prior_head=prior.head_sha[:9])
                prior = None
            if prior is not None:
                self.evidence.merge_restored(prior.payload.get("evidence", {}))
                self._candidates = list(prior.payload.get("candidates", []) or [])
                self._reused = True
                self._log("gate.reuse.applied", confirmed=len(self.evidence.confirmed))
            else:
                candidates = self.find(worktree, ref, tier.lenses)
                self._candidates = [f.to_dict() for f in candidates]
                self._log("gate.find", candidates=len(candidates), lenses=list(tier.lenses))
                self.state.write(STAGE_FIND, ref.head_sha, {"candidates": self._candidates})
                self.verify(worktree, candidates, source="lens")
                self.judge(worktree, ref)
                self.state.mark_verified(
                    ref.head_sha,
                    {
                        "evidence": self.evidence.to_payload(),
                        "candidates": self._candidates,
                    },
                    patch_id=self._pr_patch,
                    outcome="",
                )
                # The marker above is this run's own, and it is the only thing
                # _settle_state may finish on this run's behalf. The reuse
                # branch above never sets the flag: it stands on an earlier
                # run's marker, which already carries a finished outcome, and
                # stamping this run's verdict over it would make a run that
                # dispatched no reviewer the author of the verdict a later run
                # reuses.
                verified_here = True

            if not self.evidence.confirmed:
                sha = ref.head_sha
                outcome = GateOutcome.APPROVED
            else:
                sha, metric = self.converge(ref=ref, pr_tests=pr_tests)
                converged_metric = metric
                if metric.outcome == gate_metrics.OUTCOME_CONVERGED:
                    outcome = GateOutcome.APPROVED
                elif metric.outcome == gate_metrics.OUTCOME_UNTESTABLE_NEEDS_REVIEW:
                    # No test can demonstrate these, and a fixer cannot make
                    # progress on a green suite, so say who has to look.
                    reasons.append(
                        untestable_review_reason(self.evidence.confirmed)
                        or "untestable blocker(s) need human review"
                    )
                else:
                    failing = ",".join(str(f) for f in metric.failing_by_round)
                    reasons.append(
                        f"{metric.outcome} after {metric.round_count} round(s); "
                        f"failing by round: {failing}"
                    )
                    reasons.extend(
                        f"unresolved blocker: {c.get('claim') or c.get('id')}"
                        for c in self.evidence.confirmed[:3]
                    )
        except GateInfraError as exc:
            infra_failed = True
            reasons.append(str(exc)[:300])
        except (GateError, ModelPolicyError) as exc:
            reasons.append(str(exc)[:300])
        except OSError as exc:
            reasons.append(f"gate filesystem failure: {exc}"[:300])
        finally:
            remove_worktree(self.repo, worktree)
            self._settle_state(
                ref, outcome=outcome, infra_failed=infra_failed, verified_here=verified_here
            )

        return self._finish(outcome, sha, reasons, ref, metric=converged_metric)

    def _settle_state(
        self,
        ref: PullRequestRef | None,
        *,
        outcome: GateOutcome,
        infra_failed: bool,
        verified_here: bool,
    ) -> None:
        """Stamp this run's verdict onto its markers, then bound the directory.

        Runs on the way out of :meth:`run`, including its early returns. The
        markers **stay**: they are the memory a later run reuses, and clearing
        them here would make both reuse paths unreachable by construction.

        What this does is finish them. A stage cannot know the run's verdict
        when it completes, so it writes its marker with an empty outcome; that
        is what makes an *in-flight* marker unreusable, since
        :attr:`~agent_fleet.gate.state.StageState.reusable` requires a real
        outcome. Stamping it here is what turns this run's evidence into
        something the next head may stand on — and a run that died before here
        leaves it unstamped, so the next run redoes the work.

        **Only a run that verified may stamp.** Markers are keyed by head, not
        by run, so a run that returns before verification — a pre-review
        conflict, tier 0, a tier0 evidence gap — is looking at whatever the last
        run at this head left behind. Stamping there would attribute this run's
        verdict to a marker it never wrote, and the empty outcome that made a
        crashed run's marker correctly unreusable would be overwritten as if the
        crashed run had reached a verdict. The next run would then stand on
        evidence that no run ever produced, and "no confirmed blockers" would
        read as an approval of a PR nobody reviewed.
        """
        if ref is not None and verified_here:
            self.state.stamp_outcome(ref.head_sha, outcome=outcome.value, infra_failed=infra_failed)
        elif ref is not None:
            self._log("gate.state.untouched", head=ref.short_sha, reason="no verification this run")
        self.state.prune()

    def _finish(
        self,
        outcome: GateOutcome,
        sha: str,
        reasons: list[str],
        ref: PullRequestRef | None,
        *,
        metric: gate_metrics.GateMetrics | None = None,
        tier: str = "",
        metric_outcome: str = "",
        passes: int = 0,
    ) -> GateResult:
        """Record the outcome. Keeps converge()'s per-round trace when one exists.

        ``tier``, ``passes`` and ``metric_outcome`` are the STANDARD bar's: they
        are stamped onto the metrics row *before* it is appended, because that row
        is what the next run reads back to recover the pass counter. Appending
        first and stamping after would make every STANDARD run look like a fresh
        one to the next head, and the bar would never exhaust its budget.
        """
        if metric is not None:
            if outcome is GateOutcome.APPROVED and not metric_outcome:
                metric.outcome = gate_metrics.OUTCOME_CONVERGED
            if metric_outcome:
                metric.outcome = metric_outcome
            metric.tier = tier or metric.tier
            metric.passes = passes or metric.passes
            metric.reasons = list(reasons)
            metric.append_metrics()
            return self._finish_result(outcome, sha, reasons, metric)
        metric = self._metrics(
            gate_metrics.RoundMetric(round=0, head=(sha or "")[:9], failing=0),
            ref or PullRequestRef(number=self.pr_number, head_ref="", head_sha=sha, state=""),
            outcome=metric_outcome
            or (
                gate_metrics.OUTCOME_CONVERGED
                if outcome is GateOutcome.APPROVED
                else gate_metrics.OUTCOME_STALLED
            ),
            head=sha,
        )
        metric.tier = tier
        metric.passes = passes
        metric.reasons = list(reasons)
        metric.append_metrics()
        return self._finish_result(outcome, sha, reasons, metric)

    def _finish_result(
        self,
        outcome: GateOutcome,
        sha: str,
        reasons: list[str],
        metric: gate_metrics.GateMetrics,
    ) -> GateResult:
        line = status_line_for(outcome, sha, reasons)
        if self.status_file is not None:
            _write_status_line(self.status_file, line)
        self._log("gate.outcome", outcome=outcome.value, sha=sha[:9], reasons=reasons)
        return GateResult(
            outcome=outcome,
            sha=sha,
            reasons=reasons,
            metrics=metric,
            confirmed=list(self.evidence.confirmed),
            untestable=list(self.evidence.untestable),
            candidates=list(self._candidates),
            rejected=list(self.evidence.rejected_items),
            status_line=line,
            run_id=self.run_id,
            calls=self.recorder.rows(),
        )

    def _result_for(
        self,
        outcome: GateOutcome,
        sha: str,
        reasons: list[str],
        ref: PullRequestRef | None,
    ) -> GateResult:
        """A GateResult carrying the current evidence, without recording metrics.

        The funnel view of a run's evidence, for callers that want the result
        shape mid-run (a lens stage inspected in isolation, a dry run).
        """
        return GateResult(
            outcome=outcome,
            sha=sha,
            reasons=list(reasons),
            metrics=self._metrics(
                gate_metrics.RoundMetric(round=0, head=(sha or "")[:9], failing=0),
                ref or PullRequestRef(number=self.pr_number, head_ref="", head_sha=sha, state=""),
                outcome=(
                    gate_metrics.OUTCOME_CONVERGED
                    if outcome is GateOutcome.APPROVED
                    else gate_metrics.OUTCOME_STALLED
                ),
                head=sha,
            ),
            confirmed=list(self.evidence.confirmed),
            untestable=list(self.evidence.untestable),
            candidates=list(self._candidates),
            rejected=list(self.evidence.rejected_items),
            run_id=self.run_id,
            calls=self.recorder.rows(),
        )


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def tier_fields(tier: ReviewTier) -> dict[str, Any]:
    """The ``gate.tier`` log payload, including the human-readable summary line.

    The summary is a field rather than the log call's message so the numbers
    survive structured-log sinks that drop the message, and so one line can
    carry the whole reason: which tier, on how many lines, touching how many
    production-sensitive files.
    """
    return {
        "tier": tier.tier,
        "lenses": list(tier.lenses),
        "non_test_lines": tier.lines,
        "prodsensitive": tier.risky[:10],
        "n_prodsensitive": len(tier.risky),
        "summary": tier.summary,
    }


def _n_items(data: dict[str, Any]) -> int:
    """How many claims a structured answer carried (0 for scalar-shaped answers)."""
    for key in ("findings", "new_blockers", "untestable_rulings", "unresolved"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def untestable_review_reason(confirmed: list[dict[str, Any]]) -> str | None:
    """The escalation reason for a PR whose only blockers need a human.

    Returns ``None`` when there is no judge-confirmed untestable blocker, so a
    green PR still converges instead of escalating.
    """
    open_blockers = [c for c in confirmed if c.get("source") == "judge-untestable"]
    if not open_blockers:
        return None
    named = "; ".join(str(c.get("claim") or c.get("id") or "?")[:120] for c in open_blockers[:3])
    return f"untestable blocker(s) need human review: {named}"


def _file_of_node_id(node_id: str, candidates: list[str]) -> str | None:
    """Best-effort map from a failing node id back to its test file."""
    head = node_id.split("::")[0]
    if head in candidates:
        return head
    for path in candidates:
        if head.endswith(path):
            return path
    return None


def _tag_lens(findings: list[Finding], lens: str) -> list[Finding]:
    """Stamp *lens* onto findings that did not carry one."""
    return [f if f.lens else replace(f, lens=lens) for f in findings]


def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Drop repeats of the same claim across lenses (same file + same claim)."""
    import re

    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []
    for finding in findings:
        key = (
            finding.file.split("/")[-1],
            re.sub(r"\W+", " ", finding.claim.lower())[:50],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def _normalise_repo_path(path: str) -> str:
    """Trim an agent-supplied path to a repo-relative posix path.

    Agents frequently answer with an absolute path or a ``./`` prefix. Anything
    that still escapes the worktree after this normalisation is rejected by the
    caller's ``is_file()`` check on the joined path.
    """
    text = str(path).strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    return text


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _json_blob(items: list[dict[str, Any]], limit: int) -> str:
    text = "\n".join(json.dumps(item, default=str) for item in items)
    return text[:limit] if text else "(none)"


def _bound_run_log() -> Any:  # noqa: ANN401
    from agent_fleet.observability.context import get_run_log

    return get_run_log()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _load_raw_config(config_path: str | None) -> dict[str, Any]:
    """Read the machine-wide fleet.yaml as a raw dict (the gate's config source).

    The gate reads the raw mapping rather than a :class:`FleetConfig` because
    ``model_policy`` and ``gate`` are gate-owned sections that the task-dispatch
    config object has no field for. An unreadable or malformed file yields an
    empty mapping, so the gate falls back to its documented defaults.
    """
    import yaml

    from agent_fleet.fleet_paths import default_fleet_config_path

    path = Path(config_path).expanduser() if config_path else default_fleet_config_path()
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("gate: could not read %s (%s); using defaults", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def build_gate_backend(backend_name: str) -> LLMBackend:
    """Build the backend named by the gate config, not by ``default_backend``.

    The gate dispatches to two specific backends (``gate.backend`` and
    ``gate.judge_backend``), so it constructs them by name rather than going
    through the task-dispatch default.
    """
    from agent_fleet.backends import backend_is_registered
    from agent_fleet.config import load_fleet_config

    if not backend_is_registered(backend_name):
        raise ModelPolicyError(f"gate: unknown backend {backend_name!r}")
    config = load_fleet_config()
    config.default_backend = backend_name.lower()
    return make_backend(config)


def run_gate(
    *,
    repo_path: Path,
    pr_number: int,
    task_file: Path | None = None,
    status_file: Path | None = None,
    config_path: str | None = None,
    gate_dir: Path | None = None,
    run_id: str | None = None,
    use_systemd: bool | None = None,
    lane_slug: str | None = None,
) -> GateResult:
    """Run the ``gate`` pipeline for *pr_number* in *repo_path*.

    Backends and the model policy are resolved *before* any agent runs, so a
    misconfigured or policy-violating model fails in a second rather than after
    a fan-out has already spent the budget.

    *lane_slug* makes gate test file names unique per PR; when it is omitted the
    PR's own head ref supplies it, which is unique for any two distinct PRs.
    """
    repo = Path(repo_path).expanduser().resolve()
    raw = _load_raw_config(config_path)
    policy = parse_model_policy(raw)
    gate_cfg = load_gate_config(raw) or GateConfig()

    # Fail fast on policy before constructing anything expensive.
    policy.check(backend=gate_cfg.backend, model=gate_cfg.model, role=ROLE_LENS)
    if gate_cfg.enable_judge:
        policy.check(backend=gate_cfg.judge_backend, model=gate_cfg.judge_model, role=ROLE_JUDGE)

    resolved_slug = lane_slug or gate_cfg.lane_slug
    if not resolved_slug:
        fetch_base(repo, gate_cfg.base_branch)
        resolved_slug = resolve_pull_request(repo, pr_number).head_ref

    backend = build_gate_backend(gate_cfg.backend)
    judge_backend = (
        build_gate_backend(gate_cfg.judge_backend)
        if gate_cfg.judge_backend and gate_cfg.judge_backend != gate_cfg.backend
        else backend
    )

    pool_cfg = PoolConfig(
        root=default_slots_root(),
        agent_slots=gate_cfg.agent_slots,
        test_slots=gate_cfg.test_slots,
    )
    resolved_gate_dir = gate_dir or (repo / ".agent-fleet" / "gate" / str(pr_number))
    resolved_gate_dir.mkdir(parents=True, exist_ok=True)

    pipeline = GatePipeline(
        repo=repo,
        pr_number=pr_number,
        config=gate_cfg,
        policy=policy,
        backend=backend,
        judge_backend=judge_backend,
        gate_dir=resolved_gate_dir,
        task_file=Path(task_file).expanduser() if task_file else None,
        status_file=Path(status_file).expanduser() if status_file else None,
        run_id=run_id,
        agent_pool=agent_slot_pool(pool_cfg),
        test_pool=test_slot_pool(pool_cfg),
        use_systemd=use_systemd,
        lane_slug=resolved_slug,
    )
    return pipeline.run()


def _test_dir_of(changed: list[str]) -> str:
    """The directory the recheck's archived tests belong in: *test_dir*.

    The directory the PR's own changed tests already live in, so a multi-package
    repo restores the gate's test beside the tests it is meant to run with. A PR
    that changed no test of its own — or keeps them at the repository root —
    gets ``"tests"``, the directory the gate tells every verifier to write into.
    """
    dirs = {str(Path(rel).parent) for rel in changed}
    only = dirs.pop() if len(dirs) == 1 else ""
    return "tests" if only in ("", ".") else only


def run_gate_recheck(
    *,
    repo_path: Path,
    pr_number: int,
    approved_sha: str,
    head_sha: str | None = None,
    status_file: Path | None = None,
    config_path: str | None = None,
    gate_dir: Path | None = None,
    run_id: str | None = None,
    use_systemd: bool | None = None,
    lane_slug: str | None = None,
) -> GateResult:
    """Decide whether the gate's approval for *approved_sha* still holds at *head_sha*.

    The deterministic half only: the PR's own changed tests plus whatever gate
    tests the archive still holds, run on the new head with the current base
    merged in. No agent is dispatched — that is the point, since a rebase that
    did not change the change should not cost a review.

    Any failure along the way (a git call, a worktree, a test that cannot run)
    is a refusal, not an approval: ``recheck`` returning a verdict it could not
    establish is how an unapproved PR reaches the merge path.
    """
    repo = Path(repo_path).expanduser().resolve()
    raw = _load_raw_config(config_path)
    gate_cfg = load_gate_config(raw) or GateConfig()
    status = Path(status_file).expanduser() if status_file else None
    resolved_gate_dir = gate_dir or (repo / ".agent-fleet" / "gate" / str(pr_number))
    resolved_gate_dir.mkdir(parents=True, exist_ok=True)

    pipeline = GatePipeline(
        repo=repo,
        pr_number=pr_number,
        config=gate_cfg,
        policy=parse_model_policy(raw),
        backend=cast("LLMBackend", None),  # a recheck dispatches no agent
        gate_dir=resolved_gate_dir,
        status_file=status,
        run_id=run_id or f"gate-recheck-{pr_number}",
        test_pool=test_slot_pool(
            PoolConfig(
                root=default_slots_root(),
                agent_slots=0,
                test_slots=gate_cfg.test_slots,
            )
        ),
        use_systemd=use_systemd,
        lane_slug=lane_slug or gate_cfg.lane_slug,
    )

    reasons: list[str] = []
    try:
        fetch_base(repo, gate_cfg.base_branch)
        head = head_sha.strip() if head_sha else current_pr_head(repo, pr_number)
        worktree = resolved_gate_dir / "recheck-wt"
        prepare_worktree(repo, worktree, head)
        try:
            # Merge the base so the tests see what the PR will actually merge
            # into, not just the PR's own tree.
            merge_base_into(worktree, resolve_diff_base(repo, gate_cfg.base_branch))
            # The archive, not this run's evidence, is where the gate's own
            # tests live: a recheck is a fresh process, so evidence.gate_tests
            # is always empty and the one test that ever blocked the PR would
            # never be re-run. The PR's changed tests name the test directory
            # to restore them into, so the archived test lands where the rebase
            # actually broke it.
            archived = pipeline.archive.stored_tests()
            changed = changed_test_files(worktree, gate_cfg.base_branch)
            gate_tests = [f"{_test_dir_of(changed)}/{p.name}" for p in archived]
            pipeline.archive.materialise(worktree, gate_tests)
            test_files = sorted(set(changed) | set(gate_tests))
            run = pipeline._runner_for(worktree).run(test_files)
        finally:
            remove_worktree(repo, worktree)
    except (GateError, OSError) as exc:
        reasons.append(f"full gate required: recheck could not run: {str(exc)[:160]}")
        return pipeline._carry_over_result(GateOutcome.NEEDS_ESCALATION, "", reasons)

    return pipeline.recheck_carry_over(
        approved_sha=approved_sha,
        head_sha=head,
        status_file=status or (resolved_gate_dir / "absent.status"),
        test_run=run,
        test_files=test_files,
    )


def gate_metrics_summary(limit: int = 20) -> dict[str, Any]:
    """``agent-fleet gate metrics`` payload: recent runs plus aggregates."""
    rows = gate_metrics.read_metrics(limit=limit)
    return {
        "recent": rows,
        "table": gate_metrics.render_metrics_table(rows),
        "summary": gate_metrics.summarize_rows(rows),
        "path": str(gate_metrics.metrics_path()),
    }


__all__ = [
    "GateError",
    "GateInfraError",
    "GatePipeline",
    "GateResult",
    "GateTestArchive",
    "GateTestRunner",
    "TestRun",
    "build_gate_backend",
    "gate_metrics_summary",
    "run_gate",
    "run_gate_recheck",
    "status_line_for",
]
