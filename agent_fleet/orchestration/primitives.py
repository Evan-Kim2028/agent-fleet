"""Dispatch primitives: shared ThreadPoolExecutor wave execution."""

# ruff: noqa: TC001

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.types import _DispatcherLike


class DispatchPrimitives:
    """Adapter around ``_execute_task`` with wave-batched parallel dispatch."""

    def __init__(self, dispatcher: _DispatcherLike, *, max_parallel: int) -> None:
        self._dispatcher = dispatcher
        self._max_parallel = max(1, max_parallel)

    def run_one(
        self,
        index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,
    ) -> FleetTaskResult:
        return self._dispatcher._execute_task(
            index,
            task,
            batch_size=batch_size,
            same_workspace_tasks=1,
        )

    def run_many(self, tasks: list[FleetTask]) -> list[FleetTaskResult]:
        all_results: list[FleetTaskResult] = []
        limit = self._max_parallel
        for offset in range(0, len(tasks), limit):
            wave = tasks[offset : offset + limit]
            if len(wave) == 1:
                all_results.append(self.run_one(0, wave[0], batch_size=1))
                continue
            wave_results: list[FleetTaskResult | None] = [None] * len(wave)
            batch = len(wave)
            with ThreadPoolExecutor(max_workers=batch) as pool:
                futures = {
                    pool.submit(self.run_one, idx, child, batch_size=batch): idx
                    for idx, child in enumerate(wave)
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    wave_results[idx] = future.result()
            all_results.extend(r for r in wave_results if r is not None)
        return all_results
