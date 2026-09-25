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
from agent_fleet.gate.config import GateConfig, RoleTarget, load_gate_config
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
from agent_fleet.gate.inline import build_review_context
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
    openrouter_slot_pool,
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

# Config keys naming the four roles ``gate.roles:`` may pin. These are NOT the
# policy role strings: ``lens``/``verifier``/``fix`` are the *pipeline* roles the
# model policy is written against, and a role's policy role follows from it.
ROLE_FIND = "find"
ROLE_VERIFIER_CONFIG = "verify"
ROLE_FIX_CONFIG = "fix"

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
    status_line: str = ""
    run_id: str = ""

    @property
    def approved(self) -> bool:
        return self.outcome is GateOutcome.APPROVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome.value,
            "sha": self.sha,
            "reasons": list(self.reasons),
            "candidates": list(self.candidates),
            "confirmed": list(self.confirmed),
            "untestable": list(self.untestable),
            "metrics": self.metrics.to_dict() if self.metrics else {},
            "status_line": self.status_line,
        }


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
    gate_tests: list[str] = field(default_factory=list)

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
        openrouter_pool: SlotPool | None = None,
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
        self.openrouter_pool = openrouter_pool
        self.use_systemd = systemd_run_available() if use_systemd is None else use_systemd
        self.evidence = _Evidence()
        self.archive = GateTestArchive(gate_dir)
        self._candidates: list[dict[str, Any]] = []
        self._runner: GateTestRunner | None = None
        # One backend instance per distinct backend name, shared by every role
        # routed to it. ``run_gate`` fills this in; seeding the judge here keeps
        # a pipeline built by an older caller behaving exactly as before.
        self._role_backends: dict[str, LLMBackend] = {}
        if judge_backend is not None:
            self._role_backends[ROLE_JUDGE] = judge_backend
        self._inlined: dict[str, str] = {}

    # -- role routing ----------------------------------------------------

    def _role_backend(self, role: str) -> LLMBackend:
        """The backend serving *role*.

        Falls back to ``self.backend`` (the configured ``gate.backend``) when the
        role was not mapped, so a pipeline built without the per-role map behaves
        exactly as it did before per-role backends existed.
        """
        return self._role_backends.get(role, self.backend)

    def _pool_for(self, role: str) -> SlotPool | None:
        """Admission pool for *role*'s backend.

        A remote (OpenRouter) role draws from the openrouter pool so it neither
        consumes nor waits on the local cmd agent budget; everything else keeps
        the agent pool.
        """
        target = self.config.role_target(role)
        if target.needs_inline_context:
            return self.openrouter_pool
        return self.agent_pool

    def _role_context(self, role: str, worktree: Path) -> str:
        """The change rendered into the prompt, for a backend without repo tools.

        Built once per role per run and memoised: every lens in a parallel fan-out
        reviews the same change, and re-running git for each one would be N times
        the work for identical text.

        Raises :class:`GateInfraError` when the change cannot be rendered at all.
        A reviewer with no repo tools that cannot be shown the change has no
        evidence to review, and its only reachable answer is "no blockers" — so
        an unresolvable diff must fail the run closed, exactly as a dead agent
        does, rather than be recorded as a clean review.
        """
        target = self.config.role_target(role)
        if not target.needs_inline_context:
            return ""
        if role in self._inlined:
            return self._inlined[role]
        context = build_review_context(
            worktree,
            self.config.base_branch,
            max_diff_chars=self.config.inline_diff_chars,
            max_file_chars=self.config.inline_file_chars,
            max_total_chars=self.config.inline_total_chars,
        )
        if context.unavailable:
            self._log("gate.inline.unavailable", role=role, reason=context.unavailable[:200])
            raise GateInfraError(
                f"fail-closed: could not build the review context for the {role} role "
                f"({context.unavailable})"
            )
        rendered = context.render()
        self._inlined[role] = rendered
        self._log(
            "gate.inline",
            role=role,
            files=len(context.files),
            omitted=context.omitted,
            diff_truncated=context.diff_truncated,
            chars=len(rendered),
        )
        return rendered

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

    def _model_for(
        self, *, backend_name: str, model: str | None, role: str, aliases: tuple[str, ...] = ()
    ) -> str:
        """Resolve the model for one role, failing fast on a policy violation.

        *aliases* lets a config spell the role the way its own vocabulary does
        (``gate.roles.find`` vs the policy's ``lens``) without either spelling
        silently escaping the policy.
        """
        return self.policy.check(backend=backend_name, model=model, role=role, aliases=aliases)

    def _call(
        self,
        *,
        backend: LLMBackend,
        prompt: str,
        model: str,
        cwd: Path,
        timeout_s: int,
        validate: Any,  # noqa: ANN401
        mode: AgentMode = "plan",
        slot: SlotPool | None = None,
    ) -> Any:  # noqa: ANN401 - StructuredAnswer
        return call_structured(
            backend,
            prompt,
            model=model,
            cwd=cwd,
            timeout_s=timeout_s,
            validate=validate,
            mode=mode,
            slot=self.agent_pool if slot is None else slot,
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
        """
        try:
            return self._call(**kwargs)
        except StructuredCallError as exc:
            self._log(f"gate.{role}.failed", subject=subject, kind=exc.kind, error=str(exc)[:200])
            if exc.kind == "invalid" and invalid_ok:
                return None
            raise GateInfraError(
                f"fail-closed: {role} agent for {subject} gave no usable result "
                f"({exc.kind}): {str(exc)[:160]}"
            ) from exc

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
        target = self.config.role_target(ROLE_FIND)
        model = self._model_for(
            backend_name=target.backend, model=target.model, role=ROLE_LENS, aliases=(ROLE_FIND,)
        )
        backend = self._role_backend(ROLE_FIND)
        slot = self._pool_for(ROLE_FIND)
        inlined = self._role_context(ROLE_FIND, worktree)
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
                inlined_context=inlined,
            )
            answer = self._call_required(
                role="lens",
                subject=lens,
                backend=backend,
                prompt=prompt,
                model=model,
                cwd=worktree,
                timeout_s=self.config.agent_timeout_s,
                validate=validate_findings,
                slot=slot,
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
        target = self.config.role_target(ROLE_VERIFIER_CONFIG)
        model = self._model_for(
            backend_name=target.backend,
            model=target.model,
            role=ROLE_VERIFIER,
            aliases=(ROLE_VERIFIER_CONFIG,),
        )
        backend = self._role_backend(ROLE_VERIFIER_CONFIG)
        slot = self._pool_for(ROLE_VERIFIER_CONFIG)
        runner = self._runner_for(worktree)

        def _one(finding: Finding) -> None:
            self._verify_one(
                finding,
                worktree=worktree,
                runner=runner,
                model=model,
                backend=backend,
                slot=slot,
                source=source,
            )

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
        backend: LLMBackend,
        slot: SlotPool | None,
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
            backend=backend,
            prompt=prompt,
            model=model,
            cwd=worktree,
            timeout_s=self.config.agent_timeout_s,
            validate=validate_verify,
            slot=slot,
        )
        if answer is None:
            self.evidence.rejected += 1
            return
        report = VerifyReport.from_dict(answer.data)
        if report.verdict is VerifyVerdict.UNTESTABLE:
            self.evidence.untestable.append(finding.to_dict())
            self._log("gate.verify.untestable", finding=finding.id)
            return
        if report.verdict is not VerifyVerdict.CONFIRMED or not report.test_file:
            self.evidence.rejected += 1
            self._log("gate.verify.rejected", finding=finding.id, reason=report.reason[:160])
            return
        rel = _normalise_repo_path(report.test_file)
        test_path = worktree / rel
        if not test_path.is_file():
            self.evidence.rejected += 1
            self._log("gate.verify.discarded", finding=finding.id, reason="no test file")
            return
        run = runner.run([rel])
        if run.infra_error:
            self.evidence.rejected += 1
            self._log("gate.verify.discarded", finding=finding.id, reason=run.infra_error[:160])
            _unlink(test_path)
            return
        if not run.tests_failed:
            # The verifier claimed CONFIRMED but its test does not fail on a test
            # assertion. The pipeline believes the test, not the verdict.
            self.evidence.rejected += 1
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
        target = self.config.role_target(ROLE_JUDGE)
        backend = self._role_backend(ROLE_JUDGE)
        model = self._model_for(backend_name=target.backend, model=target.model, role=ROLE_JUDGE)
        prompt = judge_prompt(
            worktree=str(worktree),
            base_branch=resolve_diff_base(worktree, self.config.base_branch),
            head_sha=ref.short_sha,
            pr_number=self.pr_number,
            confirmed=_json_blob(self.evidence.confirmed, 6000),
            untestable=_json_blob(self.evidence.untestable, 6000),
            task_text=self._task_text()[:8000],
            inlined_context=self._role_context(ROLE_JUDGE, worktree),
        )
        answer = self._call_required(
            role="judge",
            subject="judge",
            backend=backend,
            prompt=prompt,
            model=model,
            cwd=worktree,
            timeout_s=self.config.judge_timeout_s,
            validate=validate_judge,
            slot=self._pool_for(ROLE_JUDGE),
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
        backend = self._role_backend(ROLE_JUDGE)
        target = self.config.role_target(ROLE_JUDGE)
        model = self._model_for(backend_name=target.backend, model=target.model, role=ROLE_JUDGE)
        prompt = recheck_prompt(
            worktree=str(worktree),
            head_sha=head_sha[:9],
            pr_number=self.pr_number,
            start_sha=start_sha[:9],
            untestable=_json_blob(untestable, 6000),
            inlined_context=self._role_context(ROLE_JUDGE, worktree),
        )
        try:
            answer = self._call(
                backend=backend,
                prompt=prompt,
                model=model,
                cwd=worktree,
                timeout_s=self.config.judge_timeout_s,
                validate=validate_recheck,
                slot=self._pool_for(ROLE_JUDGE),
            )
        except StructuredCallError as exc:
            # A failed recheck is not a pass: we could not confirm resolution.
            self._log("gate.recheck.failed", error=str(exc)[:200])
            return False
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

        push_branch = self.config.push_branch or ref.head_ref
        target = self.config.role_target(ROLE_FIX_CONFIG)
        model = self._model_for(
            backend_name=target.backend,
            model=target.model,
            role=ROLE_FIX,
            aliases=(ROLE_FIX_CONFIG,),
        )
        fix_backend = self._role_backend(ROLE_FIX_CONFIG)
        fix_slot = self._pool_for(ROLE_FIX_CONFIG)
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
                self._run_fixer(prompt, model=model, cwd=fix_wt, backend=fix_backend, slot=fix_slot)
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

    def _run_fixer(
        self,
        prompt: str,
        *,
        model: str,
        cwd: Path,
        backend: LLMBackend | None = None,
        slot: SlotPool | None = None,
    ) -> None:
        """One fix round. Free-form output (it commits and pushes), so no schema."""
        target = backend if backend is not None else self.backend
        pool = self.agent_pool if slot is None else slot
        guard = pool.slot(timeout_s=None) if pool is not None else contextlib.nullcontext()
        with guard:
            result = target.run(
                prompt,
                max_tokens=0,
                timeout_s=self.config.agent_timeout_s,
                cwd=cwd,
                model=model,
                mode="agent",
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
            status_line=line,
            run_id=self.run_id,
        )


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


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

    # Fail fast on policy before constructing anything expensive, and check
    # every role that will dispatch — not just find/judge — so a bad verify/fix
    # target also costs a second rather than a fan-out. A role the config
    # disables is exempt (see _build_role_backends). The policy role each config
    # role maps to is declared there, once, so this check and the pipeline's
    # dispatch cannot drift.
    role_backends = _build_role_backends(gate_cfg, policy)

    # ``backend`` is the pre-per-role default: any role the map does not cover
    # falls back to it, and it is what a judge-disabled run uses.
    backend = role_backends[ROLE_FIND]
    judge_backend = role_backends.get(ROLE_JUDGE, backend)

    pool_cfg = PoolConfig(
        root=default_slots_root(),
        agent_slots=gate_cfg.agent_slots,
        test_slots=gate_cfg.test_slots,
        openrouter_slots=gate_cfg.openrouter_slots,
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
        openrouter_pool=openrouter_slot_pool(pool_cfg),
        use_systemd=use_systemd,
    )
    pipeline._role_backends = role_backends
    return pipeline.run()


# Which pipeline role each ``gate.roles`` key dispatches as, for the model policy.
# ``find`` runs the lens reviewers, so the policy may spell it either way.
_ROLE_POLICY_ROLE: dict[str, tuple[str, tuple[str, ...]]] = {
    ROLE_FIND: (ROLE_LENS, (ROLE_FIND,)),
    ROLE_JUDGE: (ROLE_JUDGE, ()),
    ROLE_VERIFIER_CONFIG: (ROLE_VERIFIER, (ROLE_VERIFIER_CONFIG,)),
    ROLE_FIX_CONFIG: (ROLE_FIX, (ROLE_FIX_CONFIG,)),
}


def _build_role_backends(config: GateConfig, policy: ModelPolicy) -> dict[str, LLMBackend]:
    """Validate every dispatched role against the policy, then build one backend per name.

    The policy check for all four roles happens *before* the first backend is
    constructed, so a violation fails in a second rather than after a fan-out has
    spent the budget. One instance is built per distinct backend name and shared
    by every role routed to it, so a run does not re-initialise a session-capable
    backend once per role.

    A role the config switches off is exempt and is absent from the result. Its
    target is never dispatched, so it need not be dispatchable: a config with
    ``enable_judge: false`` and no ``judge_model`` is a valid, documented setup
    (docs/GATE.md) whose judge target is simply an unmapped model-less pair.
    """
    resolved: dict[str, LLMBackend] = {}
    targets: dict[str, RoleTarget] = {}
    # Pass 1: validate every role that will actually dispatch. Nothing is
    # constructed until they all pass, so a violation costs a second rather than
    # a fan-out.
    for role in (ROLE_FIND, ROLE_JUDGE, ROLE_VERIFIER_CONFIG, ROLE_FIX_CONFIG):
        if role == ROLE_JUDGE and not config.enable_judge:
            continue
        target = config.role_target(role)
        policy_role, aliases = _ROLE_POLICY_ROLE[role]
        policy.check(backend=target.backend, model=target.model, role=policy_role, aliases=aliases)
        targets[role] = target
    # Pass 2: one instance per distinct backend name, shared by the roles routed
    # to it, so a run does not re-initialise a session-capable backend per role.
    built: dict[str, LLMBackend] = {}
    for role, target in targets.items():
        if target.backend not in built:
            built[target.backend] = build_gate_backend(target.backend)
        resolved[role] = built[target.backend]
    return resolved


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
