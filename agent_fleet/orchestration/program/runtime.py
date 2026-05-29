"""Execute an LLM-generated orchestration program against the fleet.

This is the deep module behind a small interface. A caller hands
``run_workflow_program`` a string of Python and a dispatcher; it returns a
bounded ``ProgramRunSummary``. Behind the seam sit AST validation, the wrap
that turns top-level ``return`` into a callable, a restricted namespace, the
five primitives, thread-pooled fan-out, worktree isolation when agents run
concurrently, an agent budget, a deadline, and token accounting.

The coordination logic, which agent runs, in what order, what fans out, how
results converge, lives in the program text, not in any LLM context. The
orchestrator does not burn tokens deciding the next step. Each ``agent()`` call
returns a bounded ``AgentResult``; the full subagent transcript stays inside
the subagent. Only the program's return value crosses back to the parent.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Protocol, cast

from agent_fleet.hooks import FleetTask
from agent_fleet.orchestration.primitives import effective_capacity
from agent_fleet.orchestration.program.models import (
    AgentResult,
    ProgramExecutionError,
    ProgramRunSummary,
)
from agent_fleet.orchestration.program.validate import (
    SAFE_BUILTINS,
    validate_workflow_program,
)

if TYPE_CHECKING:
    import types
    from collections.abc import Callable

    from agent_fleet.hooks import PersonaResolver
    from agent_fleet.orchestration.types import _DispatcherLike


class _FleetLogLike(Protocol):
    def emit(self, event: str, **fields: object) -> None: ...


_SUMMARY_CEILING = 4000

# Dynamic-control bounds (Unit 4). Recursion and re-plan loops are the only two
# constructs that can grow the call tree without a fresh top-level dispatch, so
# each gets a hard cap. The agent budget is still the global resource ceiling;
# these caps stop pathological recursion that never dispatches an agent.
MAX_SUBPROGRAM_DEPTH = 3
MAX_REPLAN_ITERATIONS = 3

# Judges answer a single boolean. The schema keeps the parse trivial and gives
# the planner a stable contract.
_BRANCH_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["answer"],
}
_REPLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"done": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["done"],
}


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _stringify_result(obj: object) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)


def _bound_summary(text: str, limit: int = _SUMMARY_CEILING) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _schema_instruction(schema: dict[str, object]) -> str:
    return (
        "Return your final answer as a single JSON object that conforms to this "
        "JSON Schema. Output the JSON inside one ```json fenced block and nothing "
        f"else after it:\n{json.dumps(schema)}"
    )


def _extract_json(text: str) -> dict[str, object] | None:
    """Pull the last JSON object out of agent output, fenced or bare."""
    fence = "```json"
    if fence in text:
        after = text.rsplit(fence, 1)[1]
        end = after.find("```")
        candidate = after[:end] if end != -1 else after
        parsed = _try_load_object(candidate)
        if parsed is not None:
            return parsed
    start = text.rfind("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    parsed = _try_load_object(text[start : i + 1])
                    if parsed is not None:
                        return parsed
                    break
        start = text.rfind("{", 0, start)
    return None


def _try_load_object(candidate: str) -> dict[str, object] | None:
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _wrap_as_main(source: str) -> types.CodeType:
    """Compile *source* as the body of ``__workflow_main__`` so it can return.

    A workflow program ends with ``return <answer>``. At module level that is a
    SyntaxError, so the validated body is reparented under a synthetic function.
    Top-level statements become the function body and the final ``return`` hands
    the answer back.
    """
    tree = ast.parse(source, mode="exec")
    func = ast.FunctionDef(
        name="__workflow_main__",
        args=ast.arguments(
            posonlyargs=[],
            args=[],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        ),
        body=tree.body or [ast.Pass()],
        decorator_list=[],
        returns=None,
        type_params=[],
    )
    module = ast.Module(body=[func], type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, "<workflow-program>", "exec")


class ProgramContext:
    """Holds dispatch state for one program run and binds the five primitives.

    The primitives close over one instance. All shared mutation (the dispatch
    counter, in-flight gauge, result list, phase and log lists) is guarded by a
    single lock so ``parallel`` and ``pipeline`` threads never tear it.
    """

    def __init__(
        self,
        *,
        dispatcher: _DispatcherLike,
        persona_resolver: PersonaResolver | None,
        default_persona: str,
        default_pipeline: str,
        max_parallel: int,
        max_agents: int,
        timeout_s: float | None,
        fleet_log: _FleetLogLike | None,
        child_depth: int = 1,
    ) -> None:
        self._dispatcher = dispatcher
        self._persona_resolver = persona_resolver
        self._default_persona = default_persona
        self._default_pipeline = default_pipeline
        self._max_parallel = max(1, max_parallel)
        self._child_depth = child_depth
        self._capacity = effective_capacity(dispatcher, fallback=self._max_parallel)
        self._max_agents = max(1, max_agents)
        self._fleet_log = fleet_log
        self._lock = threading.Lock()
        self._dispatched = 0
        self._inflight = 0
        self._fanout_depth = 0
        # Sub-program recursion depth is a property of the call chain, not a
        # global count, so it lives in thread-local state. parallel/pipeline
        # seed each worker with the parent chain's depth so recursion stays
        # bounded across a fan-out, mirroring how the admission gate threads
        # ancestor depth rather than counting globally.
        self._subprogram_local = threading.local()
        self._results: list[AgentResult] = []
        self._phases: list[str] = []
        self._log: list[str] = []
        self._start = time.monotonic()
        self._deadline = (self._start + timeout_s) if timeout_s else None

    def _check_deadline(self) -> None:
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise ProgramExecutionError("workflow program exceeded its deadline")

    def _emit(self, event: str, **fields: object) -> None:
        if self._fleet_log is None:
            return
        with contextlib.suppress(Exception):
            self._fleet_log.emit(event, **fields)

    def agent(
        self,
        prompt: str,
        *,
        persona: str | None = None,
        context: str = "",
        complexity: str | None = None,
        pipeline: str | None = None,
        allowed_paths: tuple[str, ...] = (),
        title: str | None = None,
        schema: dict[str, object] | None = None,
    ) -> AgentResult:
        """Dispatch one subagent and return its bounded result.

        The subagent runs in its own context window. Concurrency is detected
        live: if another agent is already in flight, this one runs in an
        isolated worktree so parallel writers never share a tree. A lone agent
        runs in place.
        """
        self._check_deadline()
        with self._lock:
            if self._dispatched >= self._max_agents:
                raise ProgramExecutionError(
                    f"agent budget exhausted: {self._max_agents} agents dispatched"
                )
            idx = self._dispatched
            self._dispatched += 1
            self._inflight += 1
            concurrency = self._inflight
            fanout = self._fanout_depth

        try:
            full_prompt = prompt if schema is None else f"{prompt}\n\n{_schema_instruction(schema)}"
            effective_pipeline = None if complexity else (pipeline or self._default_pipeline)
            task = FleetTask(
                goal=str(full_prompt),
                context=str(context or ""),
                persona=str(persona or self._default_persona),
                pipeline=effective_pipeline,
                complexity=complexity,
                allowed_paths=tuple(allowed_paths or ()),
                title=title,
            )
            isolate = concurrency > 1 or fanout > 0
            batch = 2 if isolate else 1
            self._emit("program.agent.start", idx=idx, persona=task.persona, isolate=isolate)
            result = self._dispatcher._execute_task(
                idx, task, batch_size=batch, same_workspace_tasks=batch, depth=self._child_depth
            )
        finally:
            with self._lock:
                self._inflight -= 1

        data = _extract_json(result.summary or "") if schema is not None else None
        agent_result = AgentResult(
            status=result.status,
            summary=_bound_summary(result.summary or ""),
            persona=result.persona,
            goal=result.goal,
            duration_seconds=result.duration_seconds,
            agent_id=result.agent_id,
            data=data,
            error=result.error,
            observed_total_tokens=result.observed_total_tokens,
            task_index=result.task_index,
        )
        with self._lock:
            self._results.append(agent_result)
        self._emit("program.agent.done", idx=idx, status=agent_result.status)
        return agent_result

    def _guard(self, thunk: Callable[[], object]) -> object:
        try:
            return thunk()
        except ProgramExecutionError:
            raise
        except Exception as exc:
            with self._lock:
                self._log.append(f"thunk error: {type(exc).__name__}: {exc}")
            return None

    def _current_subdepth(self) -> int:
        return int(getattr(self._subprogram_local, "value", 0))

    def _run_at_depth(self, thunk: Callable[[], object], depth: int) -> object:
        """Run a fan-out thunk seeded with the parent chain's recursion depth.

        A new pool thread starts with empty thread-local state, so without this
        a sub-program called inside a parallel/pipeline thunk would reset to
        depth zero and escape the recursion bound. Seeding carries the chain
        depth across the fan-out.
        """
        self._subprogram_local.value = depth
        return self._guard(thunk)

    def parallel(self, thunks: list[Callable[[], object]]) -> list[object]:
        """Run thunks concurrently and return their results in order.

        A thunk that raises resolves to ``None`` rather than failing the call,
        so a partial fan-out still returns. This is a barrier: it waits for all.
        """
        self._check_deadline()
        items = list(thunks)
        if not items:
            return []
        workers = max(1, min(self._max_parallel, self._capacity, len(items)))
        parent_depth = self._current_subdepth()
        results: list[object] = [None] * len(items)
        with self._lock:
            self._fanout_depth += 1
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-par") as pool:
                future_index = {
                    pool.submit(self._run_at_depth, thunk, parent_depth): i
                    for i, thunk in enumerate(items)
                }
                for future in as_completed(future_index):
                    results[future_index[future]] = future.result()
        finally:
            with self._lock:
                self._fanout_depth -= 1
        return results

    def pipeline(
        self, items: list[object], *stages: Callable[[object, object, int], object]
    ) -> list[object]:
        """Run each item through every stage independently, no barrier between stages.

        Each stage receives ``(prev_result, original_item, index)``. A stage
        that raises drops its item to ``None`` and skips that item's remaining
        stages. Wall-clock is the slowest single-item chain, not the sum of
        per-stage maxima.
        """
        self._check_deadline()
        work = list(items)
        if not work:
            return []
        workers = max(1, min(self._max_parallel, self._capacity, len(work)))
        parent_depth = self._current_subdepth()

        def run_item(index: int, original: object) -> object:
            self._subprogram_local.value = parent_depth
            value: object = original
            for stage in stages:
                try:
                    value = stage(value, original, index)
                except ProgramExecutionError:
                    raise
                except Exception as exc:
                    with self._lock:
                        self._log.append(
                            f"pipeline stage error @item{index}: {type(exc).__name__}: {exc}"
                        )
                    return None
            return value

        results: list[object] = [None] * len(work)
        with self._lock:
            self._fanout_depth += 1
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-pipe") as pool:
                future_index = {pool.submit(run_item, i, it): i for i, it in enumerate(work)}
                for future in as_completed(future_index):
                    results[future_index[future]] = future.result()
        finally:
            with self._lock:
                self._fanout_depth -= 1
        return results

    def phase(self, title: str) -> None:
        self._check_deadline()
        with self._lock:
            self._phases.append(str(title))
            self._log.append(f"phase: {title}")
        self._emit("program.phase", title=str(title))

    def log(self, message: str) -> None:
        with self._lock:
            self._log.append(str(message))
        self._emit("program.log", message=str(message)[:500])

    def branch(
        self,
        question: str,
        if_true: Callable[[], object],
        if_false: Callable[[], object] | None = None,
        *,
        persona: str | None = None,
        context: str = "",
    ) -> object:
        """Let a judge agent decide a yes/no question, then run the chosen thunk.

        The model picks the control-flow path at runtime. A judge answers
        *question* with a boolean; on yes ``if_true()`` runs, on no
        ``if_false()`` runs when supplied, else the call returns ``None``. The
        judgment is one agent against the budget; the chosen thunk's own
        dispatches count as usual. A thunk that raises resolves to ``None``.
        """
        self._check_deadline()
        verdict = self.agent(
            f"Answer this yes/no question with a JSON boolean.\n\nQuestion: {question}",
            persona=persona,
            context=context,
            schema=_BRANCH_SCHEMA,
        )
        chose_true = bool(verdict.data and verdict.data.get("answer"))
        with self._lock:
            self._log.append(f"branch -> {chose_true}: {str(question)[:80]}")
        self._emit("program.branch", chose_true=chose_true)
        if chose_true:
            return self._guard(if_true)
        if if_false is not None:
            return self._guard(if_false)
        return None

    def replan(
        self,
        goal: str,
        step: Callable[[int, object], object],
        *,
        max_iterations: int = MAX_REPLAN_ITERATIONS,
        persona: str | None = None,
    ) -> list[object]:
        """Loop a step under model control until the goal is met or the cap.

        Each round runs ``step(iteration, last_result)`` then asks a judge
        whether *goal* is satisfied. The loop stops on the first satisfied
        verdict or after ``MAX_REPLAN_ITERATIONS`` rounds, whichever comes
        first; the judge does not run after the final round. Returns the list of
        per-round results so the program can keep the last or fold them. A round
        whose step raises records ``None`` for that round and keeps looping.
        """
        self._check_deadline()
        rounds = max(1, min(int(max_iterations), MAX_REPLAN_ITERATIONS))
        history: list[object] = []
        for i in range(rounds):
            self._check_deadline()
            last = history[-1] if history else None
            try:
                result = step(i, last)
            except ProgramExecutionError:
                raise
            except Exception as exc:
                with self._lock:
                    self._log.append(f"replan step {i} error: {type(exc).__name__}: {exc}")
                result = None
            history.append(result)
            if i + 1 >= rounds:
                break
            verdict = self.agent(
                f"Goal:\n{goal}\n\nLatest result:\n"
                f"{_bound_summary(_stringify_result(result), 2000)}\n\n"
                "Is the goal fully met? Answer with a JSON boolean: "
                "true to stop, false to iterate.",
                persona=persona,
                schema=_REPLAN_SCHEMA,
            )
            if verdict.data and verdict.data.get("done"):
                self._emit("program.replan.satisfied", iteration=i)
                break
        return history

    def subprogram(self, source: str) -> object:
        """Generate and run a nested workflow program at runtime.

        This is runtime-dynamic orchestration: a running program can build a new
        program string, typically from what its agents just found, and execute
        it here. The nested program shares this run's dispatcher, agent budget,
        deadline, and result list, so the only newly bounded resource is
        recursion depth, capped at ``MAX_SUBPROGRAM_DEPTH`` per call chain. The
        source is validated by the same validator with zero relaxation; an
        invalid or too-deep sub-program raises ``ProgramExecutionError``. The
        sub-program's return value crosses back like any other value.
        """
        self._check_deadline()
        depth = self._current_subdepth()
        if depth >= MAX_SUBPROGRAM_DEPTH:
            raise ProgramExecutionError(
                f"subprogram recursion exceeded MAX_SUBPROGRAM_DEPTH={MAX_SUBPROGRAM_DEPTH}"
            )
        validation = validate_workflow_program(source)
        if not validation.ok:
            raise ProgramExecutionError(
                "subprogram failed validation: " + "; ".join(validation.errors)
            )
        self._emit("program.subprogram.start", depth=depth + 1)
        code = _wrap_as_main(source)
        nested_ns = self.build_namespace()
        self._subprogram_local.value = depth + 1
        try:
            exec(code, nested_ns)  # restricted namespace, validated source
            return cast("Callable[[], object]", nested_ns["__workflow_main__"])()
        finally:
            self._subprogram_local.value = depth

    def build_namespace(self) -> dict[str, object]:
        safe = {
            name: getattr(builtins, name) for name in SAFE_BUILTINS if hasattr(builtins, name)
        }
        return {
            "__builtins__": safe,
            "agent": self.agent,
            "parallel": self.parallel,
            "pipeline": self.pipeline,
            "phase": self.phase,
            "log": self.log,
            "branch": self.branch,
            "replan": self.replan,
            "subprogram": self.subprogram,
        }


def run_workflow_program(
    source: str,
    *,
    dispatcher: _DispatcherLike,
    persona_resolver: PersonaResolver | None = None,
    default_persona: str = "coder",
    default_pipeline: str = "simple",
    max_parallel: int = 16,
    max_agents: int = 64,
    timeout_s: float | None = None,
    fleet_log: _FleetLogLike | None = None,
    child_depth: int = 1,
) -> ProgramRunSummary:
    """Validate, then run an orchestration program, returning a bounded summary.

    Invalid source returns a ``status='error'`` summary without dispatching
    anything. A program that raises is caught and reported, with whatever
    agents already ran still counted. The two token figures are the headline:
    work done across agents versus what the parent must read.
    """
    validation = validate_workflow_program(source)
    if not validation.ok:
        return ProgramRunSummary(
            status="error",
            error="program failed validation: " + "; ".join(validation.errors),
        )

    context = ProgramContext(
        dispatcher=dispatcher,
        persona_resolver=persona_resolver,
        default_persona=default_persona,
        default_pipeline=default_pipeline,
        max_parallel=max_parallel,
        max_agents=max_agents,
        timeout_s=timeout_s,
        fleet_log=fleet_log,
        child_depth=child_depth,
    )
    namespace = context.build_namespace()
    start = time.monotonic()

    status = "completed"
    error: str | None = None
    result: object = None
    try:
        code = _wrap_as_main(source)
        exec(code, namespace)  # restricted namespace, validated source
        result = cast("Callable[[], object]", namespace["__workflow_main__"])()
    except ProgramExecutionError as exc:
        status, error = "error", str(exc)
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"

    with context._lock:
        agent_results = tuple(context._results)
        phases = tuple(context._phases)
        log_lines = tuple(context._log)

    tokens_across = sum((r.observed_total_tokens or 0) for r in agent_results)
    tokens_to_parent = _estimate_tokens(_stringify_result(result))

    return ProgramRunSummary(
        status=status,
        result=result,
        error=error,
        agents_dispatched=len(agent_results),
        agents_ok=sum(1 for r in agent_results if r.ok),
        phases=phases,
        log=log_lines,
        tokens_across_agents=tokens_across,
        tokens_to_parent=tokens_to_parent,
        duration_seconds=round(time.monotonic() - start, 2),
        agent_results=agent_results,
    )
