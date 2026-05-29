"""Admission controller for CLI batch dispatch (``agent-fleet run --tasks``).

Issue-loop watcher admission uses ``agent_fleet.capacity.FleetCapacityGate`` instead.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass(frozen=True)
class ResourceTier:
    name: str
    ram_gb: int
    max_concurrent: int


@dataclass(frozen=True)
class Token:
    id: str
    tier: str
    ram_gb: int


class AdmissionController:
    def __init__(self, ram_budget_gb: int, tiers: dict[str, ResourceTier]) -> None:
        self._ram_budget_gb = ram_budget_gb
        self._tiers = tiers
        self._in_flight: dict[str, Token] = {}

    def try_admit(self, tier: str) -> Token | None:
        tier_obj = self._tiers[tier]
        if self.in_flight_count_for_tier(tier) >= tier_obj.max_concurrent:
            return None
        if self.in_flight_ram_gb + tier_obj.ram_gb > self._ram_budget_gb:
            return None
        token = Token(id=str(uuid.uuid4()), tier=tier, ram_gb=tier_obj.ram_gb)
        self._in_flight[token.id] = token
        return token

    def release(self, token: Token) -> None:
        self._in_flight.pop(token.id, None)

    @property
    def in_flight_ram_gb(self) -> int:
        return sum(t.ram_gb for t in self._in_flight.values())

    def in_flight_count_for_tier(self, tier: str) -> int:
        return sum(1 for t in self._in_flight.values() if t.tier == tier)

    def capacity_for(self, tier: str) -> int:
        """Max simultaneous admits for *tier* — the count cap and the RAM budget,
        whichever binds. Mirrors the two ``try_admit`` deny conditions."""
        tier_obj = self._tiers[tier]
        return min(tier_obj.max_concurrent, self._ram_budget_gb // tier_obj.ram_gb)


class AdmissionDenied(Exception):
    """Raised by ``AdmissionGate`` only for the structural-deadlock case: a
    caller whose ancestors already hold every slot can never be admitted, so the
    gate refuses instead of blocking forever."""


class AdmissionGate:
    """Blocking front door to an ``AdmissionController``: overflow queues.

    A caller that cannot be admitted right now waits until a slot frees rather
    than being denied, so a wide fan-out drains through the RAM ceiling instead
    of failing. The single exception is the structural-deadlock case. ``depth``
    is the number of admission tokens already held by the caller's *ancestors*
    (0 at top level, +1 per in-process orchestration nesting level). When
    ``depth >= capacity`` every slot is held by an ancestor that will not
    release until this subtree finishes, so blocking would deadlock; the gate
    denies instead, preserving liveness for that degenerate configuration.

    The controller is touched only under this gate's condition variable, so the
    gate is the controller's sole serialization point — no external lock.
    """

    def __init__(self, controller: AdmissionController, *, tier: str) -> None:
        self._controller = controller
        self._tier = tier
        self._cond = threading.Condition()

    def acquire_token(self, *, depth: int = 0) -> Token:
        with self._cond:
            cap = self._controller.capacity_for(self._tier)
            if depth >= cap:
                raise AdmissionDenied(
                    f"nesting depth {depth} >= capacity {cap}: "
                    "all admission slots held by ancestors"
                )
            while True:
                token = self._controller.try_admit(self._tier)
                if token is not None:
                    return token
                self._cond.wait()

    def release(self, token: Token) -> None:
        with self._cond:
            self._controller.release(token)
            self._cond.notify_all()

    @contextlib.contextmanager
    def acquire(self, *, depth: int = 0) -> Generator[Token]:
        token = self.acquire_token(depth=depth)
        try:
            yield token
        finally:
            self.release(token)
