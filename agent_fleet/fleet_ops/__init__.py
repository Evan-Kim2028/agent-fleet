"""Multi-operator lane manager — replaces the bash fleet drivers.

``agent-fleet lane run`` drives one lane end to end (worktree, implementer,
**guaranteed PR**, gate, status line); ``agent-fleet lanes`` gives a
cross-operator view of every lane and can stop exactly one of them.

Two operator sessions work the same repos concurrently, so the pieces that make
that safe are not optional: the repo/PR :mod:`binding` that stops a lane judging
the wrong PR, the per-operator :mod:`registry`, and :mod:`stop`'s refusal to
signal anything but one lane's own recorded process group.
"""

from agent_fleet.fleet_ops.binding import BindingResult, LaneBinding, resolve
from agent_fleet.fleet_ops.config import (
    FleetOpsConfig,
    OperatorSpec,
    load_fleet_ops_config,
    load_fleet_ops_config_from_repo,
)
from agent_fleet.fleet_ops.guarantee import GuaranteeResult, ensure_pull_request
from agent_fleet.fleet_ops.lazyexit import LazyExitVerdict, judge_run
from agent_fleet.fleet_ops.models import (
    ModelPolicyError,
    enforce_implementation_model,
    resolve_engine_model,
)
from agent_fleet.fleet_ops.registry import LaneRecord
from agent_fleet.fleet_ops.runner import LaneRunResult, run_lane
from agent_fleet.fleet_ops.status import render_table, status_dicts, status_rows
from agent_fleet.fleet_ops.statusfile import HookResult, approved_line, escalation_line
from agent_fleet.fleet_ops.stop import StopResult, stop_lane, stop_lane_by_name
from agent_fleet.fleet_ops.worktree import WorktreeResult, ensure_lane_worktree

__all__ = [
    "BindingResult",
    "FleetOpsConfig",
    "GuaranteeResult",
    "HookResult",
    "LaneBinding",
    "LaneRecord",
    "LaneRunResult",
    "LazyExitVerdict",
    "ModelPolicyError",
    "OperatorSpec",
    "StopResult",
    "WorktreeResult",
    "approved_line",
    "enforce_implementation_model",
    "ensure_lane_worktree",
    "ensure_pull_request",
    "escalation_line",
    "judge_run",
    "load_fleet_ops_config",
    "load_fleet_ops_config_from_repo",
    "render_table",
    "resolve",
    "resolve_engine_model",
    "run_lane",
    "status_dicts",
    "status_rows",
    "stop_lane",
    "stop_lane_by_name",
]
