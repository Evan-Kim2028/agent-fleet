"""Deterministic instrumented dispatcher for stretch-testing the orchestration runtime.

No real LLM or composer is called. All behaviour is in-process and deterministic.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from agent_fleet.hooks import FleetTask, FleetTaskResult


@dataclass
class _CallRecord:
    index: int
    label: str
    start: float
    end: float
    result_chars: int


class Recorder:
    """Thread-safe log of every _execute_task call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[_CallRecord] = []

    def _append(self, record: _CallRecord) -> None:
        with self._lock:
            self._records.append(record)

    def records(self) -> list[_CallRecord]:
        with self._lock:
            return list(self._records)

    def parallelism_factor(self) -> float:
        """sum(end-start) / (max(end) - min(start)) — >1 means overlap."""
        recs = self.records()
        min_for_parallelism = 2
        if len(recs) < min_for_parallelism:
            return 1.0
        total_work = sum(r.end - r.start for r in recs)
        wall = max(r.end for r in recs) - min(r.start for r in recs)
        if wall <= 0:
            return 1.0
        return total_work / wall

    def total_result_chars(self) -> int:
        return sum(r.result_chars for r in self.records())


class InstrumentedDispatcher:
    """_DispatcherLike that simulates work with configurable latency and output size.

    Parameters
    ----------
    latency_s:
        Sleep duration per call (default 0.02s). Keeps tests fast while making
        threaded parallelism measurable.
    result_chars:
        Length of the synthetic result text returned per call (default 2000).
    fail_labels:
        Set of task labels (persona or title) whose calls return status 'error'.
    raise_labels:
        Set of task labels whose calls raise RuntimeError instead of returning.
    recorder:
        Shared Recorder instance; a new one is created if not supplied.
    """

    def __init__(
        self,
        *,
        latency_s: float = 0.02,
        result_chars: int = 2000,
        fail_labels: set[str] | None = None,
        raise_labels: set[str] | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self._latency_s = latency_s
        self._result_chars = result_chars
        self._fail_labels = fail_labels or set()
        self._raise_labels = raise_labels or set()
        self._counter_lock = threading.Lock()
        self._counter = 0
        self.recorder: Recorder = recorder if recorder is not None else Recorder()

    def _next_index(self) -> int:
        with self._counter_lock:
            idx = self._counter
            self._counter += 1
            return idx

    def _label(self, task: FleetTask) -> str:
        return task.title or task.persona

    def _execute_task(
        self,
        task_index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,  # noqa: ARG002
        same_workspace_tasks: int = 1,  # noqa: ARG002
        handoff: object = None,  # noqa: ARG002
        base_branch: str | None = None,  # noqa: ARG002
    ) -> FleetTaskResult:
        label = self._label(task)
        call_idx = self._next_index()
        start = time.monotonic()

        time.sleep(self._latency_s)

        end = time.monotonic()
        result_text = "x" * self._result_chars

        self.recorder._append(
            _CallRecord(
                index=call_idx,
                label=label,
                start=start,
                end=end,
                result_chars=self._result_chars,
            )
        )

        if label in self._raise_labels:
            raise RuntimeError(f"injected failure for label={label!r}")

        status = "error" if label in self._fail_labels else "completed"
        error = f"injected error for label={label!r}" if status == "error" else None

        return FleetTaskResult(
            task_index=task_index,
            persona=task.persona,
            goal=task.goal,
            status=status,
            summary=result_text if status == "completed" else None,
            error=error,
            duration_seconds=end - start,
            observed_total_tokens=len(result_text) // 4,
        )
