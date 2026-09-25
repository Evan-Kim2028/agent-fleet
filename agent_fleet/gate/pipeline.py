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
from typing import TYPE_CHECKING, Any

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
from agent_fleet.gate import metrics as gate_metrics
from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.gitops import (
    GateError,
    PullRequestRef,
    changed_test_files,
    current_pr_head,
    fetch_base,
    prepare_worktree,
    remove_worktree,
    resolve_diff_base,
    resolve_pull_request,
    worktree_head_sha,
)
from agent_fleet.gate.prompts import (
    find_prompt,
    fix_prompt,
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
from agent_fleet.gate.structured import StructuredCallError, call_structured
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
        self.evidence = _Evidence()
        self.archive = GateTestArchive(gate_dir)
        self.recorder = GateCallRecorder(gate_dir)
        self._candidates: list[dict[str, Any]] = []
        self._runner: GateTestRunner | None = None

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
                exit_code=1,
                duration_s=getattr(exc, "duration_s", 0.0),
                lens=str(kwargs.get("lens", "")),
            )
            self._log(f"gate.{role}.failed", subject=subject, kind=exc.kind, error=str(exc)[:200])
            if exc.kind == "invalid" and invalid_ok:
                return None
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
        """
        pr_tests = changed_test_files(worktree, self.config.base_branch)
        if not pr_tests:
            return []
        runner = self._runner_for(worktree)
        run = runner.run(pr_tests)
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

    # -- step 1 ----------------------------------------------------------

    def find(self, worktree: Path, ref: PullRequestRef) -> list[Finding]:
        """Run the lens reviewers in parallel and dedupe their candidate claims."""
        model = self._model_for(
            backend_name=self.config.backend, model=self.config.model, role=ROLE_LENS
        )
        task_text = self._task_text()
        lenses = self.config.lenses[: self.config.max_parallel_lenses]

        def _one(lens: str) -> list[Finding]:
            prompt = find_prompt(
                lens=lens,
                focus=self.config.focus_for(lens),
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
                timeout_s=self.config.agent_timeout_s,
                validate=validate_findings,
                list_key="findings",
            )
            return _tag_lens(FindingsReport.from_dict(answer.data).findings, lens)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(lenses), 1)) as pool:
            batches = list(pool.map(_one, lenses))

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
        prompt = verify_prompt(
            finding=finding,
            worktree=str(worktree),
            base_branch=resolve_diff_base(worktree, self.config.base_branch),
            head_sha=(self._head_sha(worktree) or "")[:9],
            pr_number=self.pr_number,
            test_dir_hint=runner.test_dir_hint(finding.file or "x"),
            pytest_cmd_hint=runner.pytest_hint("tests/test_gate_x.py"),
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
            timeout_s=self.config.agent_timeout_s,
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
            timeout_s=self.config.judge_timeout_s,
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

        Returns True when nothing is left unresolved.
        """
        if not self.config.enable_judge or self.judge_backend is None:
            return True
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
                timeout_s=self.config.judge_timeout_s,
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
                exit_code=1,
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
        if run.count == 0 and untestable_open:
            # Nothing fails and the only blockers are untestable ones no local
            # test can demonstrate. A fixer cannot make progress on a green
            # test set, so spending a round here just produced the misleading
            # "cap after 1 round(s)" verdict: escalate and name what a human
            # has to look at.
            return current, self._metrics(
                metric,
                ref,
                outcome=gate_metrics.OUTCOME_UNTESTABLE_NEEDS_REVIEW,
                rounds=rounds,
            )

        push_branch = self.config.push_branch or ref.head_ref
        model = self._model_for(
            backend_name=self.config.backend, model=self.config.model, role=ROLE_FIX
        )
        outcome = gate_metrics.OUTCOME_CAP

        for round_number in range(1, max(1, self.config.max_fix_rounds) + 1):
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
                    pytest_cmd_hint=self._runner_for(fix_wt).pytest_hint("tests/test_gate_x.py"),
                    task_text=self._task_text()[:6000],
                )
                self._run_fixer(prompt, model=model, cwd=fix_wt)
            finally:
                remove_worktree(self.repo, fix_wt)

            fetch_base(self.repo, self.config.base_branch)
            new_head = current_pr_head(self.repo, self.pr_number)
            if new_head == current:
                outcome = gate_metrics.OUTCOME_NO_PUSH
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
            if needs_recheck and not self.recheck_untestable(head_wt, start_sha, current):
                outcome = gate_metrics.OUTCOME_UNTESTABLE_UNRESOLVED
            elif needs_recheck:
                outcome = gate_metrics.OUTCOME_CONVERGED
        finally:
            remove_worktree(self.repo, head_wt)

        return current, self._metrics(metric, ref, outcome=outcome, rounds=rounds, head=current)

    def _run_fixer(self, prompt: str, *, model: str, cwd: Path) -> None:
        """One fix round. Free-form output (it commits and pushes), so no schema."""
        guard = (
            self.agent_pool.slot(timeout_s=None)
            if self.agent_pool is not None
            else (contextlib.nullcontext())
        )
        with guard:
            result = self.backend.run(
                prompt,
                max_tokens=0,
                timeout_s=self.config.agent_timeout_s,
                cwd=cwd,
                model=model,
                mode="agent",
            )
        self.recorder.record(
            stage="fix",
            model=model,
            raw=result.stdout or "",
            parsed=None,
            parse_error="" if result.exit_code == 0 else (result.stderr or "")[:400],
            exit_code=result.exit_code,
            duration_s=getattr(result, "duration_s", 0.0),
        )
        if result.exit_code != 0:
            self._log("gate.fix.failed", error=(result.stderr or "")[:200])

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

    # -- entry point -----------------------------------------------------

    def run(self) -> GateResult:
        """Execute the whole gate. Always returns a result; never raises."""
        reasons: list[str] = []
        ref: PullRequestRef | None = None
        worktree = self.gate_dir / "wt"
        outcome = GateOutcome.NEEDS_ESCALATION
        sha = ""
        converged_metric: gate_metrics.GateMetrics | None = None
        try:
            fetch_base(self.repo, self.config.base_branch)
            ref = resolve_pull_request(self.repo, self.pr_number)
            if not ref.is_open:
                reasons.append(f"PR #{ref.number} is {ref.state}, not OPEN")
                return self._finish(outcome, "", reasons, ref)
            self._log("gate.start", pr=ref.number, head=ref.short_sha, branch=ref.head_ref)

            prepare_worktree(self.repo, worktree, ref.head_sha)
            self._runner = self._runner_for(worktree)
            pr_tests = self.run_pr_tests(worktree)
            candidates = self.find(worktree, ref)
            self._candidates = [f.to_dict() for f in candidates]
            self._log("gate.find", candidates=len(candidates), lenses=list(self.config.lenses))
            self.verify(worktree, candidates, source="lens")
            self.judge(worktree, ref)

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
            reasons.append(str(exc)[:300])
        except (GateError, ModelPolicyError) as exc:
            reasons.append(str(exc)[:300])
        except OSError as exc:
            reasons.append(f"gate filesystem failure: {exc}"[:300])
        finally:
            remove_worktree(self.repo, worktree)

        return self._finish(outcome, sha, reasons, ref, metric=converged_metric)

    def _finish(
        self,
        outcome: GateOutcome,
        sha: str,
        reasons: list[str],
        ref: PullRequestRef | None,
        *,
        metric: gate_metrics.GateMetrics | None = None,
    ) -> GateResult:
        """Record the outcome. Keeps converge()'s per-round trace when one exists."""
        if metric is not None:
            if outcome is GateOutcome.APPROVED:
                metric.outcome = gate_metrics.OUTCOME_CONVERGED
            metric.reasons = list(reasons)
            metric.append_metrics()
            return self._finish_result(outcome, sha, reasons, metric)
        metric = self._metrics(
            gate_metrics.RoundMetric(round=0, head=(sha or "")[:9], failing=0),
            ref or PullRequestRef(number=self.pr_number, head_ref="", head_sha=sha, state=""),
            outcome=(
                gate_metrics.OUTCOME_CONVERGED
                if outcome is GateOutcome.APPROVED
                else gate_metrics.OUTCOME_STALLED
            ),
            head=sha,
        )
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
) -> GateResult:
    """Run the ``gate`` pipeline for *pr_number* in *repo_path*.

    Backends and the model policy are resolved *before* any agent runs, so a
    misconfigured or policy-violating model fails in a second rather than after
    a fan-out has already spent the budget.
    """
    repo = Path(repo_path).expanduser().resolve()
    raw = _load_raw_config(config_path)
    policy = parse_model_policy(raw)
    gate_cfg = load_gate_config(raw) or GateConfig()

    # Fail fast on policy before constructing anything expensive.
    policy.check(backend=gate_cfg.backend, model=gate_cfg.model, role=ROLE_LENS)
    if gate_cfg.enable_judge:
        policy.check(backend=gate_cfg.judge_backend, model=gate_cfg.judge_model, role=ROLE_JUDGE)

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
    )
    return pipeline.run()


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
    "status_line_for",
]
