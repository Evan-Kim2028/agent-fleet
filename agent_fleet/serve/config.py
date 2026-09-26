"""Serve configuration — one place for every threshold, all with safe defaults.

Where the section lives is settled here, because getting it wrong is silent and
therefore expensive. ``fleet_ops:`` is a *repo* config section: it is read by
walking ``.agent-fleet.yaml`` under a repo root, and the global
``~/.agent-fleet/fleet.yaml`` has no such key at all. Serve supervises
processes across repos, so its configuration is machine-level, and it is read
from the global ``fleet.yaml`` under a **top-level ``serve:``** key.

A repo may still carry a ``fleet_ops.serve`` block, because that is where an
operator looking at a repo will look for it. The precedence is explicit and
total —

1. the file named by ``--serve-config``, if given;
2. the global ``fleet.yaml`` (or ``$AGENT_FLEET_CONFIG``);
3. ``serve:`` in the repo's ``.agent-fleet.yaml``;
4. ``fleet_ops.serve`` in the repo's ``.agent-fleet.yaml`` (compat);
5. built-in defaults.

— and when a file is named explicitly but has no serve section, that is an
**error**, not a silent fall back to defaults. A supervisor that runs for three
days on thresholds the operator believes they configured is the failure mode
this rule exists to prevent.

Every threshold has a default that is safe without configuration: floors rather
than ceilings, a 20-minute stage timeout rather than an hour, and a
crash-loop budget that trips before a broken component is restarted a hundred
times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_fleet.serve.capacity import (
    DEFAULT_MEMORY_HIGH_RATIO,
    DEFAULT_MEMORY_LOW_RATIO,
    CapacityBounds,
    CapacityPolicy,
    Watermarks,
)
from agent_fleet.serve.pressure import DEFAULT_CGROUP_NAME

#: Component roles serve supervises, and the config key each one's command
#: template lives under. A missing command means the component is disabled —
#: serve runs the roles it has commands for and reports the rest as disabled
#: rather than inventing a default and failing to spawn it.
COMPONENTS = ("dispatcher", "merger", "janitor")


@dataclass(frozen=True)
class ComponentSpec:
    """One supervised role: what to run, how long to wait, when to give up."""

    name: str
    #: Command template. Placeholders expanded at spawn: ``{operator}``,
    #: ``{capacity_file}``, ``{max_lanes}``, ``{max_gates}``, ``{gates_priority}``,
    #: ``{serve_dir}``. A template with no placeholders is run verbatim.
    command: str | None = None
    #: Seconds between restart attempts at the start of the backoff schedule.
    backoff_initial_s: float = 5.0
    #: Ceiling for the exponential backoff.
    backoff_max_s: float = 300.0
    #: Crashes allowed inside ``crash_window_minutes`` before the component is
    #: declared crash-looping and stopped for a human to look at.
    crash_threshold: int = 5
    crash_window_minutes: int = 15
    #: Zero-progress restart budget: restarts allowed inside
    #: ``no_progress_window_minutes`` before the watchdog stops asking.
    no_progress_restarts: int = 2
    no_progress_window_minutes: int = 30
    #: Seconds the supervisor waits for a clean exit during shutdown.
    shutdown_grace_s: float = 10.0

    @property
    def enabled(self) -> bool:
        return bool(self.command and self.command.strip())


@dataclass(frozen=True)
class WatchdogConfig:
    """Thresholds for the five self-healing rules."""

    #: (a) A stage whose tracked output file has not grown in this long is
    #: declared stuck. Applies per stage, so a long gate does not eat a long
    #: lane's budget.
    stage_timeout_minutes: dict[str, int] = field(
        default_factory=lambda: {
            "lane": 180,
            "gate": 90,
            "fix": 60,
            "rebase": 45,
            "merge": 60,
        }
    )
    #: (b) A blocking command older than this, whose parent is gone, is an orphan.
    orphan_minutes: int = 60
    #: (c) A lock whose holder pid is dead is released after this grace, so a
    #: just-released lock is never stolen out from under its previous holder.
    stale_lock_minutes: int = 15
    #: (d) Two components each holding what the other wants for this long is a
    #: deadlock; the older claim is released.
    deadlock_minutes: int = 20
    #: (e) A component with queued work and no events for this long is wedged.
    no_progress_minutes: int = 30
    #: Per-tick remediation budget. Without it, a crash that left twenty stale
    #: children would make one watchdog tick take minutes, and the watchdog
    #: would then trip its own no-progress rule.
    max_remediations_per_tick: int = 5
    #: Seconds of grace to wait before escalating TERM to KILL, spent once per
    #: tick rather than once per pid.
    kill_grace_s: float = 5.0
    #: How many times the owning component may retry a stage the watchdog killed.
    stage_retry_budget: int = 1

    def timeout_for(self, stage: str) -> int:
        return self.stage_timeout_minutes.get(stage, 120)


@dataclass(frozen=True)
class ServeConfig:
    """The whole ``serve:`` section."""

    operator: str = ""
    tick_seconds: float = 15.0
    #: Seconds the supervisor waits for children to exit cleanly on shutdown,
    #: before escalating to a group KILL.
    shutdown_grace_s: float = 10.0
    #: The agents cgroup. A bare slice name is resolved under systemd's nesting
    #: prefixes; see :func:`agent_fleet.serve.pressure.resolve_cgroup`.
    cgroup: str = DEFAULT_CGROUP_NAME
    #: Watchdog cadence, as a multiple of the tick. A wedged watchdog is
    #: invisible, so it runs on its own schedule.
    watchdog_every_ticks: int = 1
    capacity: CapacityPolicy = field(default_factory=CapacityPolicy)
    components: dict[str, ComponentSpec] = field(
        default_factory=lambda: {name: ComponentSpec(name=name) for name in COMPONENTS}
    )
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig)
    #: Where queue items are read from when bootstrapping. Empty means the
    #: components own the board entirely.
    queue_files: tuple[str, ...] = ()
    #: Human review queue for fence/owner escalations.
    decisions_file: str | None = None
    #: Rolling window for ``serve status`` throughput, in hours.
    throughput_window_hours: float = 1.0

    def component(self, name: str) -> ComponentSpec:
        return self.components.get(name) or ComponentSpec(name=name)

    @property
    def enabled_components(self) -> tuple[ComponentSpec, ...]:
        return tuple(spec for spec in self.components.values() if spec.enabled)

    @property
    def marks(self) -> Watermarks:
        return self.capacity.marks

    @property
    def bounds(self) -> CapacityBounds:
        return self.capacity.bounds


def _int(value: Any, default: int) -> int:  # noqa: ANN401
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except TypeError, ValueError:
        return default


def _float(value: Any, default: float) -> float:  # noqa: ANN401
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except TypeError, ValueError:
        return default


def _str(value: Any) -> str | None:  # noqa: ANN401
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_bounds(section: dict[str, Any]) -> CapacityBounds:
    defaults = CapacityBounds()
    floors = section.get("floors") or {}
    ceilings = section.get("ceilings") or {}
    return CapacityBounds(
        lanes_floor=_int(floors.get("lanes"), defaults.lanes_floor),
        lanes_ceiling=_int(ceilings.get("lanes"), defaults.lanes_ceiling),
        gates_floor=_int(floors.get("gates"), defaults.gates_floor),
        gates_ceiling=_int(ceilings.get("gates"), defaults.gates_ceiling),
        tests_floor=_int(floors.get("tests"), defaults.tests_floor),
        tests_ceiling=_int(ceilings.get("tests"), defaults.tests_ceiling),
        typecheck_floor=_int(floors.get("typecheck"), defaults.typecheck_floor),
        typecheck_ceiling=_int(ceilings.get("typecheck"), defaults.typecheck_ceiling),
    )


def _load_marks(section: dict[str, Any]) -> Watermarks:
    defaults = Watermarks()
    low = section.get("psi_low")
    high = section.get("psi_high")
    mem_low = section.get("memory_low_ratio")
    mem_high = section.get("memory_high_ratio")
    return Watermarks(
        low=_float(low, defaults.low),
        high=_float(high, defaults.high),
        memory_low=_float(mem_low, DEFAULT_MEMORY_LOW_RATIO),
        memory_high=_float(mem_high, DEFAULT_MEMORY_HIGH_RATIO),
    )


def _load_capacity(section: dict[str, Any]) -> CapacityPolicy:
    defaults = CapacityPolicy()
    return CapacityPolicy(
        bounds=_load_bounds(section),
        marks=_load_marks(section),
        step=max(1, _int(section.get("step"), defaults.step)),
        decrease=min(1.0, max(0.05, _float(section.get("decrease"), defaults.decrease))),
        starvation_ticks=max(1, _int(section.get("starvation_ticks"), defaults.starvation_ticks)),
    )


def _load_component(name: str, raw: Any) -> ComponentSpec:  # noqa: ANN401
    defaults = ComponentSpec(name=name)
    if not isinstance(raw, dict):
        return replace_command(defaults, _str(raw))
    timeouts = raw.get("timeouts") or {}
    crash = timeouts.get("crash") or {}
    no_progress = timeouts.get("no_progress") or {}
    return ComponentSpec(
        name=name,
        command=_str(raw.get("command")),
        backoff_initial_s=_float(raw.get("backoff_initial_s"), defaults.backoff_initial_s),
        backoff_max_s=_float(raw.get("backoff_max_s"), defaults.backoff_max_s),
        crash_threshold=max(1, _int(crash.get("threshold"), defaults.crash_threshold)),
        crash_window_minutes=max(
            1, _int(crash.get("window_minutes"), defaults.crash_window_minutes)
        ),
        no_progress_restarts=max(
            0, _int(no_progress.get("restarts"), defaults.no_progress_restarts)
        ),
        no_progress_window_minutes=max(
            1, _int(no_progress.get("window_minutes"), defaults.no_progress_window_minutes)
        ),
        shutdown_grace_s=_float(raw.get("shutdown_grace_s"), defaults.shutdown_grace_s),
    )


def replace_command(spec: ComponentSpec, command: str | None) -> ComponentSpec:
    return ComponentSpec(
        name=spec.name,
        command=command,
        backoff_initial_s=spec.backoff_initial_s,
        backoff_max_s=spec.backoff_max_s,
        crash_threshold=spec.crash_threshold,
        crash_window_minutes=spec.crash_window_minutes,
        no_progress_restarts=spec.no_progress_restarts,
        no_progress_window_minutes=spec.no_progress_window_minutes,
        shutdown_grace_s=spec.shutdown_grace_s,
    )


def _load_watchdog(raw: Any) -> WatchdogConfig:  # noqa: ANN401
    defaults = WatchdogConfig()
    if not isinstance(raw, dict):
        return defaults
    timeouts = raw.get("timeouts") or {}
    stage_timeouts = defaults.stage_timeout_minutes.copy()
    if isinstance(timeouts, dict):
        for stage, minutes in timeouts.items():
            if isinstance(minutes, dict):
                minutes = minutes.get("minutes")
            parsed = _int(minutes, 0)
            if parsed > 0:
                stage_timeouts[str(stage)] = parsed
    budget = raw.get("remediation_budget") or {}
    return WatchdogConfig(
        stage_timeout_minutes=stage_timeouts,
        orphan_minutes=max(1, _int(raw.get("orphan_minutes"), defaults.orphan_minutes)),
        stale_lock_minutes=max(1, _int(raw.get("stale_lock_minutes"), defaults.stale_lock_minutes)),
        deadlock_minutes=max(1, _int(raw.get("deadlock_minutes"), defaults.deadlock_minutes)),
        no_progress_minutes=max(
            1, _int(raw.get("no_progress_minutes"), defaults.no_progress_minutes)
        ),
        max_remediations_per_tick=max(
            1, _int(budget.get("max_per_tick"), defaults.max_remediations_per_tick)
        ),
        kill_grace_s=_float(raw.get("kill_grace_s"), defaults.kill_grace_s),
        stage_retry_budget=max(0, _int(raw.get("stage_retry_budget"), defaults.stage_retry_budget)),
    )


def parse_serve_config(section: Any, *, operator: str = "") -> ServeConfig | None:  # noqa: ANN401
    """Build a :class:`ServeConfig` from a raw ``serve:`` mapping."""
    if not isinstance(section, dict):
        return None
    components_raw = section.get("components")
    components: dict[str, ComponentSpec] = {}
    for name in COMPONENTS:
        entry = None
        if isinstance(components_raw, dict):
            entry = components_raw.get(name)
        components[name] = _load_component(name, entry)
    queue = section.get("queue_files") or []
    capacity_section = section.get("capacity")
    if not isinstance(capacity_section, dict):
        capacity_section = {}
    return ServeConfig(
        operator=operator,
        tick_seconds=max(1.0, _float(section.get("tick_seconds"), 15.0)),
        shutdown_grace_s=max(0.1, _float(section.get("shutdown_grace_s"), 10.0)),
        cgroup=_str(section.get("cgroup")) or DEFAULT_CGROUP_NAME,
        watchdog_every_ticks=max(1, _int(section.get("watchdog_every_ticks"), 1)),
        capacity=_load_capacity(capacity_section),
        components=components,
        watchdog=_load_watchdog(section.get("watchdog")),
        queue_files=tuple(str(q) for q in queue if str(q).strip()),
        decisions_file=_str(section.get("decisions_file")),
        throughput_window_hours=max(0.1, _float(section.get("throughput_window_hours"), 1.0)),
    )


def _read_yaml(path: Path) -> dict[str, Any] | None:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, yaml.YAMLError:
        return None
    return raw if isinstance(raw, dict) else None


def _repo_sections(raw: dict[str, Any]) -> dict[str, Any] | None:
    """``serve:`` first, then ``fleet_ops.serve`` — both repo-local spellings."""
    top = raw.get("serve")
    if isinstance(top, dict):
        return top
    fleet_ops = raw.get("fleet_ops")
    if isinstance(fleet_ops, dict):
        nested = fleet_ops.get("serve")
        if isinstance(nested, dict):
            return nested
    return None


def load_serve_config(
    *,
    operator: str = "",
    config_path: Path | None = None,
    repo_root: Path | None = None,
) -> ServeConfig:
    """Resolve serve configuration from every source, in the documented order.

    An explicitly named *config_path* that has no serve section raises
    :class:`ServeConfigError`. Implicit sources (the global fleet.yaml, the
    repo config) fall through to the next source without complaint, because a
    machine that has never configured serve should still get working defaults.
    """
    sections: list[tuple[dict[str, Any], Path]] = []

    if config_path is not None:
        raw = _read_yaml(config_path)
        if raw is not None:
            section = _repo_sections(raw)
            if section is not None:
                sections.append((section, config_path))
            else:
                raise ServeConfigError(
                    f"{config_path} has no `serve:` (or `fleet_ops.serve:`) section. "
                    f"Point --serve-config at a file that configures serve, or drop the "
                    f"flag to use the global fleet.yaml."
                )
    else:
        from agent_fleet.fleet_paths import default_fleet_config_path

        global_path = default_fleet_config_path()
        raw = _read_yaml(global_path)
        if raw is not None and isinstance(raw.get("serve"), dict):
            sections.append((raw["serve"], global_path))

    if repo_root is not None:
        from agent_fleet.repo import REPO_CONFIG_NAMES

        for name in REPO_CONFIG_NAMES:
            path = Path(repo_root) / name
            if not path.exists():
                continue
            repo_raw = _read_yaml(path)
            if repo_raw is None:
                continue
            section = _repo_sections(repo_raw)
            if section is not None:
                sections.append((section, path))
            break

    for section, _path in sections:
        parsed = parse_serve_config(section, operator=operator)
        if parsed is not None:
            return parsed

    return ServeConfig(operator=operator)


class ServeConfigError(Exception):
    """An explicitly named config file does not configure serve."""


__all__ = [
    "COMPONENTS",
    "ComponentSpec",
    "ServeConfig",
    "ServeConfigError",
    "WatchdogConfig",
    "load_serve_config",
    "parse_serve_config",
]
