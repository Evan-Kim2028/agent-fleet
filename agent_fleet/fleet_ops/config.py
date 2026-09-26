"""Per-operator lane configuration from a repo's ``.agent-fleet.yaml``.

Declarative, additive section. Nothing else in the codebase reads it, so a
repo that omits ``fleet_ops:`` is unaffected::

    fleet_ops:
      base_branch: main
      stall_minutes: 20
      baseline_skip_hooks: [ruff-format, pyright]
      operators:
        documents-0e:
          engine: cmd
          push_branch: fb/{lane}
          task_file: prompts/{lane}.task.md
        documents-1d:
          engine: devin
          push_branch: fb/{lane}
          on_approved: "cp $STATUS $REPO/reviews/$PR-$SHA9.md"

``baseline_skip_hooks`` is deliberately scoped to ``fleet_ops`` rather than to
the global repo config: skipping a hook is a decision about the *lane manager's
own auto-commit*, and the allowed ids differ per repo. Hooks are never disabled
globally — every commit the manager makes still runs every hook not listed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_fleet.repo import REPO_CONFIG_NAMES

DEFAULT_BASE_BRANCH = "main"
DEFAULT_STALL_MINUTES = 20
DEFAULT_PUSH_BRANCH = "fb/{lane}"
DEFAULT_ENGINE = "cmd"

#: Queue-dispatch defaults. ``max_gates`` is deliberately far below the shell
#: driver's 10: that driver released eighteen gates in one tick and drove the box
#: to load 200. See docs/FLEET-OPS.md.
DEFAULT_MAX_LANES = 8
DEFAULT_MAX_GATES = 4

_FIELDS = (
    "engine",
    "push_branch",
    "task_file",
    "on_approved",
    "on_escalated",
    "base_branch",
    "stall_minutes",
    "judge_engine",
)


def _optional_str(value: Any) -> str | None:  # noqa: ANN401
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class OperatorSpec:
    """One operator session's lane defaults."""

    name: str
    engine: str = DEFAULT_ENGINE
    #: Push target. ``{lane}`` is expanded at use time.
    push_branch: str = DEFAULT_PUSH_BRANCH
    #: Where this operator's task files live. ``{lane}`` is expanded.
    task_file: str | None = None
    #: Shell command run after a lane is approved. Not a template by default —
    #: it is executed with a small, documented environment (see statusfile.py).
    on_approved: str | None = None
    #: Shell command run when a lane escalates — for a stall, a lazy exit, a
    #: failed commit, or a rejected gate. Separate from ``on_approved`` because
    #: the two outcomes have different downstream contracts: an approval tells a
    #: shipper to publish, an escalation tells a monitor to stop and look.
    on_escalated: str | None = None
    #: Engine the gate should use as its judge. Per-operator so one session can
    #: pin every model it uses (``documents-1d`` pins cmd/space-bunny even for
    #: the judge) while another permits the grok judge.
    judge_engine: str | None = None
    base_branch: str | None = None
    stall_minutes: int | None = None

    def branch_for(self, lane: str) -> str:
        """Expand this operator's push target for *lane*."""
        return expand_template(self.push_branch, lane=lane)

    def task_file_for(self, lane: str) -> str | None:
        if not self.task_file:
            return None
        return expand_template(self.task_file, lane=lane)

    def stall_minutes_for(self, default: int) -> int:
        return self.stall_minutes if self.stall_minutes is not None else default


@dataclass(frozen=True)
class DispatchConfig:
    """The ``fleet_ops.dispatch:`` block — how a queue is run.

    ``psi_avg10_max`` is the throttle that replaced the shell driver's
    ``--max-load``. Load average is the wrong signal on a box whose agents run
    under a cgroup CPU quota: a quota-throttled task is still *running* as far as
    the load average is concerned, so load reads high while the machine is idle
    but stalled, and the dispatcher refused to launch anything for forty
    minutes. CPU PSI measures saturation instead, and :mod:`.pressure` fails
    open when it cannot be read.
    """

    max_lanes: int = DEFAULT_MAX_LANES
    max_gates: int = DEFAULT_MAX_GATES
    #: Percent of the last 10s that at least one task was runnable-but-waiting.
    psi_avg10_max: float = 25.0
    #: Override the agents-slice ``cpu.pressure`` (tests, unusual cgroup trees).
    psi_path: str | None = None
    #: Cluster launch order; unknown clusters sort last.
    cluster_order: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdmissionPoolConfig:
    """The ``fleet_ops.admission:`` block — lane subprocess budgets.

    ``shared_dir`` is what makes the pools shared rather than per-operator: two
    operators pointing at the same directory contend for the same flock slots,
    which is the point, because the constraint is the hardware.
    """

    tests: int = 12
    typecheck: int = 4
    shared_dir: str | None = None
    nice: int = 5


@dataclass(frozen=True)
class FleetOpsConfig:
    """The whole ``fleet_ops:`` section."""

    base_branch: str = DEFAULT_BASE_BRANCH
    stall_minutes: int = DEFAULT_STALL_MINUTES
    #: Hook ids the manager's auto-commit may pass via ``SKIP=``. Every other
    #: hook still runs. Never a blanket disable.
    baseline_skip_hooks: tuple[str, ...] = ()
    #: Extra standing fences for this repo, appended to the house rules. A repo
    #: can *add* rules; it can never shorten them.
    fences: tuple[str, ...] = ()
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    admission: AdmissionPoolConfig = field(default_factory=AdmissionPoolConfig)
    operators: dict[str, OperatorSpec] = field(default_factory=dict)

    def operator(self, name: str) -> OperatorSpec | None:
        return self.operators.get(name)

    def skip_env(self) -> dict[str, str]:
        """The env overlay for the manager's commit (``SKIP=`` for named hooks)."""
        if not self.baseline_skip_hooks:
            return {}
        return {"SKIP": ",".join(self.baseline_skip_hooks)}


def expand_template(template: str, *, lane: str = "", operator: str = "") -> str:
    """Expand the ``{lane}`` / ``{operator}`` placeholders used in config values.

    Deliberately not ``str.format``: operator-supplied values are plain strings
    from YAML, and a stray brace in one should not raise.
    """
    return template.replace("{lane}", lane).replace("{operator}", operator)


def _parse_operator(name: str, raw: Any) -> OperatorSpec | None:  # noqa: ANN401
    if not isinstance(raw, dict):
        return None
    name = str(name).strip()
    if not name:
        return None
    stall = raw.get("stall_minutes")
    return OperatorSpec(
        name=name,
        engine=str(raw.get("engine") or DEFAULT_ENGINE).strip().lower(),
        push_branch=str(raw.get("push_branch") or DEFAULT_PUSH_BRANCH).strip(),
        task_file=_optional_str(raw.get("task_file")),
        on_approved=_optional_str(raw.get("on_approved")),
        on_escalated=_optional_str(raw.get("on_escalated")),
        judge_engine=_optional_str(raw.get("judge_engine")),
        base_branch=_optional_str(raw.get("base_branch")),
        stall_minutes=int(stall) if isinstance(stall, int) and stall > 0 else None,
    )


def _positive_int(value: Any, fallback: int) -> int:  # noqa: ANN401
    try:
        number = int(value)
    except TypeError, ValueError:
        return fallback
    return number if number > 0 else fallback


def _parse_dispatch(raw: Any) -> DispatchConfig:  # noqa: ANN401
    defaults = DispatchConfig()
    if not isinstance(raw, dict):
        return defaults
    clusters = raw.get("cluster_order") or ()
    if isinstance(clusters, str):
        clusters = (clusters,)
    try:
        psi_max = float(raw.get("psi_avg10_max", defaults.psi_avg10_max))
    except TypeError, ValueError:
        psi_max = defaults.psi_avg10_max
    return DispatchConfig(
        max_lanes=_positive_int(raw.get("max_lanes"), defaults.max_lanes),
        max_gates=_positive_int(raw.get("max_gates"), defaults.max_gates),
        psi_avg10_max=psi_max,
        psi_path=_optional_str(raw.get("psi_path")),
        cluster_order=tuple(str(c).strip() for c in clusters if str(c).strip()),
    )


def _parse_admission(raw: Any) -> AdmissionPoolConfig:  # noqa: ANN401
    defaults = AdmissionPoolConfig()
    if not isinstance(raw, dict):
        return defaults
    nice = defaults.nice
    nice_raw = raw.get("nice")
    if isinstance(nice_raw, int):
        nice = nice_raw
    return AdmissionPoolConfig(
        tests=_positive_int(raw.get("tests"), defaults.tests),
        typecheck=_positive_int(raw.get("typecheck"), defaults.typecheck),
        shared_dir=_optional_str(raw.get("shared_dir")),
        nice=nice,
    )


def load_fleet_ops_config(raw: dict[str, Any] | None) -> FleetOpsConfig | None:
    """Parse the ``fleet_ops:`` block out of an already-loaded repo config dict.

    Returns ``None`` when the section is absent or disabled, so a repo without
    lane-manager config behaves exactly as before.
    """
    section = (raw or {}).get("fleet_ops")
    if not section or section is False:
        return None
    if not isinstance(section, dict):
        return None

    stall = section.get("stall_minutes")
    hooks = section.get("baseline_skip_hooks") or []
    fences = section.get("fences") or []
    operators_raw = section.get("operators") or {}
    operators: dict[str, OperatorSpec] = {}
    if isinstance(operators_raw, dict):
        for name, entry in operators_raw.items():
            spec = _parse_operator(str(name), entry)
            if spec is not None:
                operators[spec.name] = spec

    return FleetOpsConfig(
        base_branch=str(section.get("base_branch") or DEFAULT_BASE_BRANCH).strip(),
        stall_minutes=int(stall) if isinstance(stall, int) and stall > 0 else DEFAULT_STALL_MINUTES,
        baseline_skip_hooks=tuple(str(h).strip() for h in hooks if str(h).strip()),
        fences=tuple(str(f).strip() for f in fences if str(f).strip()),
        dispatch=_parse_dispatch(section.get("dispatch")),
        admission=_parse_admission(section.get("admission")),
        operators=operators,
    )


def load_fleet_ops_config_from_repo(repo_root: Any) -> FleetOpsConfig | None:  # noqa: ANN401
    """Read ``.agent-fleet.yaml`` under *repo_root* and parse its ``fleet_ops:``.

    Mirrors ``workstreams.cli.load_repo_workstreams``: reads the YAML directly
    rather than requiring the field on ``RepoConfig``, so the new section stays
    purely additive.
    """
    from pathlib import Path

    import yaml

    root = Path(repo_root)
    for name in REPO_CONFIG_NAMES:
        path = root / name
        if not path.exists():
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            return load_fleet_ops_config(raw)
    return None


def effective_stall_minutes(config: FleetOpsConfig | None, operator: str | None) -> int:
    """Resolve the stall threshold: operator override > global > default."""
    default = config.stall_minutes if config is not None else DEFAULT_STALL_MINUTES
    if config is not None and operator is not None:
        spec = config.operator(operator)
        if spec is not None:
            return spec.stall_minutes_for(default)
    return default


__all__ = [
    "DEFAULT_BASE_BRANCH",
    "DEFAULT_ENGINE",
    "DEFAULT_MAX_GATES",
    "DEFAULT_MAX_LANES",
    "DEFAULT_PUSH_BRANCH",
    "DEFAULT_STALL_MINUTES",
    "AdmissionPoolConfig",
    "DispatchConfig",
    "FleetOpsConfig",
    "OperatorSpec",
    "effective_stall_minutes",
    "expand_template",
    "load_fleet_ops_config",
    "load_fleet_ops_config_from_repo",
]
