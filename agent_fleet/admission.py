"""Admission controller — gate parallel agent spawns."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


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
