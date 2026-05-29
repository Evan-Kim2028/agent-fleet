"""Dispatch primitives: shared ThreadPoolExecutor wave execution."""

# ruff: noqa: TC001

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from agent_fleet.hooks import FleetTask, FleetTaskResult
from agent_fleet.orchestration.types import _DispatcherLike

_RAM_GB_PER_AGENT = 4  # mirrors admission.ResourceTier("agent", ram_gb=4)


def effective_capacity(dispatcher: object, *, fallback: int) -> int:
    """Thread-pool sizing hint: the admission capacity (``max_parallel`` bounded
    by the RAM budget).

    This is only a worker-count hint, not the real bound. The AdmissionGate is
    authoritative and queues any overflow, and because a fanning-out parent holds
    its own token in the shared controller, children already see one fewer free
    slot without any reservation arithmetic here. Reads ``dispatcher.config``
    when present (the real FleetDispatcher); returns ``fallback`` for stand-ins
    that expose no config.
    """
    config = getattr(dispatcher, "config", None)
    if config is None:
        return max(1, fallback)
    cap = int(config.max_parallel)
    ram_budget_gb = getattr(config, "ram_budget_gb", None)
    if ram_budget_gb is not None:
        cap = min(cap, int(ram_budget_gb) // _RAM_GB_PER_AGENT)
    return max(1, cap)


class DispatchPrimitives:
    """Adapter around ``_execute_task`` with wave-batched parallel dispatch.

    ``depth`` is the admission-nesting level at which children are dispatched;
    these helpers only ever fan out below a token-holding parent, so it defaults
    to 1. The AdmissionGate uses it to queue overflow rather than deny.
    """

    def __init__(self, dispatcher: _DispatcherLike, *, max_parallel: int) -> None:
        self._dispatcher = dispatcher
        self._max_parallel = max(1, max_parallel)

    def run_one(
        self,
        index: int,
        task: FleetTask,
        *,
        batch_size: int = 1,
        depth: int = 1,
    ) -> FleetTaskResult:
        return self._dispatcher._execute_task(
            index,
            task,
            batch_size=batch_size,
            same_workspace_tasks=1,
            depth=depth,
        )

    def run_many(self, tasks: list[FleetTask], *, depth: int = 1) -> list[FleetTaskResult]:
        all_results: list[FleetTaskResult] = []
        limit = self._max_parallel
        for offset in range(0, len(tasks), limit):
            wave = tasks[offset : offset + limit]
            if len(wave) == 1:
                all_results.append(self.run_one(0, wave[0], batch_size=1, depth=depth))
                continue
            wave_results: list[FleetTaskResult | None] = [None] * len(wave)
            batch = len(wave)
            with ThreadPoolExecutor(max_workers=batch) as pool:
                futures = {
                    pool.submit(self.run_one, idx, child, batch_size=batch, depth=depth): idx
                    for idx, child in enumerate(wave)
                }
                for future in as_completed(futures):
                    idx = futures[future]
                    wave_results[idx] = future.result()
            all_results.extend(r for r in wave_results if r is not None)
        return all_results
