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
from agent_fleet.orchestration.program.models import (
    AgentResult,
    ProgramExecutionError,
    ProgramRunSummary,
)
from agent_fleet.orchestration.primitives import effective_capacity
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
    ) -> None:
        self._dispatcher = dispatcher
        self._persona_resolver = persona_resolver
        self._default_persona = default_persona
        self._default_pipeline = default_pipeline
        self._max_parallel = max(1, max_parallel)
        self._capacity = effective_capacity(
            dispatcher, fallback=self._max_parallel, reserved=1
        )
        self._max_agents = max(1, max_agents)
        self._fleet_log = fleet_log
        self._lock = threading.Lock()
        self._dispatched = 0
        self._inflight = 0
        self._fanout_depth = 0
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
                idx, task, batch_size=batch, same_workspace_tasks=batch
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
        results: list[object] = [None] * len(items)
        with self._lock:
            self._fanout_depth += 1
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wf-par") as pool:
                future_index = {pool.submit(self._guard, thunk): i for i, thunk in enumerate(items)}
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

        def run_item(index: int, original: object) -> object:
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
