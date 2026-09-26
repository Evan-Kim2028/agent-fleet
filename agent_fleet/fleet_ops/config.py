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
      admission:
        shared_dir: ~/.agent-fleet/admission
        tests: 12
        typecheck: 4
        nice: 5

``admission:`` is the lane subprocess budget — the shared ``uv run pytest`` /
``pyright`` slot pools every operator contends for. Every key is optional and
every one of them defaults to the machine-global budget, because admission is a
throttle: a knob a repo omits must never keep a lane from running.

``baseline_skip_hooks`` is deliberately scoped to ``fleet_ops`` rather than to
the global repo config: skipping a hook is a decision about the *lane manager's
own auto-commit*, and the allowed ids differ per repo. Hooks are never disabled
globally — every commit the manager makes still runs every hook not listed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_fleet.fleet_ops.admission import AdmissionConfig
from agent_fleet.repo import REPO_CONFIG_NAMES

DEFAULT_BASE_BRANCH = "main"
DEFAULT_STALL_MINUTES = 20
DEFAULT_PUSH_BRANCH = "fb/{lane}"
DEFAULT_ENGINE = "cmd"

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
    operators: dict[str, OperatorSpec] = field(default_factory=dict)
    #: Lane admission budget. ``AdmissionConfig()`` defaults are the whole
    #: config: a repo that says nothing about admission gets the machine-global
    #: pools, and a missing knob throttles nobody rather than aborting the lane.
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)

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


def _parse_admission(raw: Any) -> AdmissionConfig:  # noqa: ANN401
    """Parse the ``admission:`` sub-section, defaulting every absent knob.

    Admission is a throttle. Every value is optional and an unparseable one
    falls back to the :class:`AdmissionConfig` default, because a lane that
    cannot start is strictly worse than a lane that runs unthrottled.
    """
    if not isinstance(raw, dict):
        return AdmissionConfig()
    defaults = AdmissionConfig()
    shared = _optional_str(raw.get("shared_dir"))
    return AdmissionConfig(
        shared_dir=Path(shared) if shared else None,
        tests=_positive_int(raw.get("tests"), defaults.tests),
        typecheck=_positive_int(raw.get("typecheck"), defaults.typecheck),
        nice=_positive_int(raw.get("nice"), defaults.nice),
        wait_s=_positive_float(raw.get("wait_s"), defaults.wait_s),
    )


def _positive_int(value: Any, default: int) -> int:  # noqa: ANN401
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _positive_float(value: Any, default: float) -> float:  # noqa: ANN401
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return default


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
        operators=operators,
        admission=_parse_admission(section.get("admission")),
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
    "DEFAULT_PUSH_BRANCH",
    "DEFAULT_STALL_MINUTES",
    "FleetOpsConfig",
    "OperatorSpec",
    "effective_stall_minutes",
    "expand_template",
    "load_fleet_ops_config",
    "load_fleet_ops_config_from_repo",
]
