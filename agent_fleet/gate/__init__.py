"""The ``gate`` pipeline: evidence-based pre-merge approval for an open PR.

Public surface:
    run_gate              — run the gate for one PR
    GateResult            — the verdict plus its evidence
    GatePipeline          — the step-by-step pipeline (for tests and reuse)
    gate_metrics_summary  — the ``gate metrics`` rollup
    status_line_for       — the automerge status line format
"""

from agent_fleet.gate.config import GateConfig, load_gate_config
from agent_fleet.gate.gitops import GateError, PullRequestRef
from agent_fleet.gate.metrics import GateMetrics, RoundMetric
from agent_fleet.gate.pipeline import (
    GateInfraError,
    GatePipeline,
    GateResult,
    gate_metrics_summary,
    run_gate,
    status_line_for,
)

__all__ = [
    "GateConfig",
    "GateError",
    "GateInfraError",
    "GateMetrics",
    "GatePipeline",
    "GateResult",
    "PullRequestRef",
    "RoundMetric",
    "gate_metrics_summary",
    "load_gate_config",
    "run_gate",
    "status_line_for",
]
