"""Orchestration settings — auto-decompose and plan preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrchestrationConfig:
    """Controls automatic task decomposition and child dispatch."""

    enabled: bool = True
    auto_dispatch_children: bool = True
    auto_dispatch_dag: bool = True
    auto_dispatch_program: bool = True
    preflight_on_code_review: bool = False
    max_decomposition_depth: int = 1
    default_child_pipeline: str = "code_review"
    default_dag_pipeline: str = "code_review"
    dag_upstream_context_chars: int = 2000

    @classmethod
    def defaults(cls) -> OrchestrationConfig:
        return cls()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> OrchestrationConfig:
        if not raw or not isinstance(raw, dict):
            return cls.defaults()
        if raw.get("enabled") is False:
            return cls(
                enabled=False,
                auto_dispatch_children=False,
                auto_dispatch_dag=False,
                auto_dispatch_program=False,
                preflight_on_code_review=False,
            )
        return cls(
            enabled=bool(raw.get("enabled", True)),
            auto_dispatch_children=bool(raw.get("auto_dispatch_children", True)),
            auto_dispatch_dag=bool(raw.get("auto_dispatch_dag", True)),
            auto_dispatch_program=bool(raw.get("auto_dispatch_program", True)),
            preflight_on_code_review=bool(raw.get("preflight_on_code_review", False)),
            max_decomposition_depth=max(1, int(raw.get("max_decomposition_depth", 1))),
            default_child_pipeline=str(raw.get("default_child_pipeline") or "code_review"),
            default_dag_pipeline=str(raw.get("default_dag_pipeline") or "code_review"),
            dag_upstream_context_chars=max(0, int(raw.get("dag_upstream_context_chars", 2000))),
        )


def resolve_orchestration_config(raw: dict[str, Any] | None) -> OrchestrationConfig:
    section = (raw or {}).get("orchestration")
    if section is False:
        return OrchestrationConfig(
            enabled=False,
            auto_dispatch_children=False,
            auto_dispatch_dag=False,
            auto_dispatch_program=False,
            preflight_on_code_review=False,
        )
    if isinstance(section, dict):
        return OrchestrationConfig.from_dict(section)
    return OrchestrationConfig.defaults()
