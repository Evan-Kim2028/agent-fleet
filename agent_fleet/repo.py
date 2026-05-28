"""Per-repository configuration discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from agent_fleet.level_up.config import LevelUpConfig, load_level_up_config
from agent_fleet.pr_review.config import PrReviewConfig, load_pr_review_config

if TYPE_CHECKING:
    from agent_fleet.capacity.config import FleetCapacity
    from agent_fleet.code_review.config import CodeReviewConfig
    from agent_fleet.config import FleetConfig
    from agent_fleet.issue_loop.config import BacklogDispatcherConfig, IssueDispatchConfig
    from agent_fleet.orchestration.config import OrchestrationConfig
    from agent_fleet.pr_loop.config import PrLoopConfig
    from agent_fleet.schedule.config import ScheduleConfig

REPO_CONFIG_NAMES = (
    ".agent-fleet.yaml",
    ".agent-fleet.yml",
    "agent-fleet.yaml",
    "agent-fleet.yml",
)


@dataclass
class RepoConfig:
    """Configuration loaded from a repo's .agent-fleet.yaml."""

    repo_root: Path
    config_root: Path | None = None
    config_path: Path | None = None
    state_root: Path | None = None
    target_configs: tuple[RepoConfig, ...] = ()
    name: str = ""
    default_persona: str = "coder"
    default_branch: str = "main"
    personas_dir: Path | None = None
    skills_dirs: tuple[Path, ...] = ()
    use_worktree: bool = False
    worktree_base: Path | None = None
    verify_commands: list[str] = field(default_factory=list)
    persona_verify_commands: dict[str, tuple[str, ...]] = field(default_factory=dict)
    worktree_bootstrap_commands: list[str] = field(default_factory=list)
    commit_preflight_commands: list[str] = field(default_factory=list)
    test_command: str | None = None
    lint_command: str | None = None
    typecheck_command: str | None = None
    persona_scope_allowlist: dict[str, tuple[str, ...]] = field(default_factory=dict)
    cross_cutting_groups: tuple[frozenset[str], ...] = ()
    critical_path_prefixes: tuple[str, ...] = ()
    spine_overrides: dict[str, Any] = field(default_factory=dict)
    pr_review: PrReviewConfig | None = None
    pr_loop: PrLoopConfig | None = None
    code_review: CodeReviewConfig | None = None
    issue_dispatch: IssueDispatchConfig | None = None
    backlog_dispatcher: BacklogDispatcherConfig | None = None
    schedules: ScheduleConfig | None = None
    capacity: FleetCapacity | None = None
    orchestration: OrchestrationConfig | None = None
    level_up: LevelUpConfig | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.repo_root.name

    def verify_commands_for(self, persona: str | None) -> list[str]:
        """Per-persona verify commands when declared, else the repo-wide set.

        Lets a monorepo scope lint/test to a persona's lane (e.g. ``ruff check
        packages/lakestore``) instead of failing a scoped task on pre-existing
        debt elsewhere in the tree.
        """
        if persona and persona in self.persona_verify_commands:
            return list(self.persona_verify_commands[persona])
        return list(self.verify_commands)


def find_repo_config(start: Path | str | None = None) -> RepoConfig | None:
    """Walk up from *start* (or cwd) looking for .agent-fleet.yaml."""
    current = Path(start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in [current, *current.parents]:
        for name in REPO_CONFIG_NAMES:
            path = directory / name
            if path.exists():
                return load_repo_config(path)
        if (directory / ".git").exists():
            break
    return None


def resolve_repo_config(workspace: Path | str) -> RepoConfig | None:
    """Resolve target repo config from workspace, honoring AGENT_FLEET_TARGET_CONFIG."""
    import os

    explicit = os.environ.get("AGENT_FLEET_TARGET_CONFIG")
    if explicit:
        return load_repo_config(explicit)
    return find_repo_config(workspace)


def load_repo_config(
    path: Path | str,
    *,
    controller_root: Path | None = None,
) -> RepoConfig:
    config_path = Path(path).expanduser().resolve()
    config_root = config_path.parent
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}

    workspace_raw = raw.get("workspace")
    repo_root = Path(str(workspace_raw)).expanduser().resolve() if workspace_raw else config_root

    state_root_raw = raw.get("state_root")
    if state_root_raw:
        state_root = Path(str(state_root_raw)).expanduser().resolve()
    elif workspace_raw and controller_root is not None:
        state_root = controller_root.resolve()
    elif workspace_raw and config_root.name == "targets":
        state_root = config_root.parent.resolve()
    else:
        state_root = repo_root

    verify_commands = list(raw.get("verify_commands") or [])
    persona_verify_commands = {
        str(persona): tuple(str(cmd) for cmd in (cmds or []))
        for persona, cmds in (raw.get("persona_verify_commands") or {}).items()
    }
    worktree_bootstrap_commands = list(raw.get("worktree_bootstrap_commands") or [])
    commit_preflight_commands = list(raw.get("commit_preflight_commands") or [])
    test_command = raw.get("test_command")
    lint_command = raw.get("lint_command")
    typecheck_command = raw.get("typecheck_command")
    if test_command and test_command not in verify_commands:
        verify_commands.append(test_command)
    if lint_command and lint_command not in verify_commands:
        verify_commands.append(lint_command)
    if typecheck_command and typecheck_command not in verify_commands:
        verify_commands.append(typecheck_command)

    personas_dir_raw = raw.get("personas_dir")
    personas_dir = (repo_root / personas_dir_raw).resolve() if personas_dir_raw else None

    skills_dirs: list[Path] = []
    skills_dir_raw = raw.get("skills_dir")
    if skills_dir_raw:
        skills_dirs.append((repo_root / str(skills_dir_raw)).resolve())
    for entry in raw.get("skills_dirs") or []:
        skills_dirs.append((repo_root / str(entry)).resolve())

    scope_map: dict[str, tuple[str, ...]] = {}
    for persona, paths in (raw.get("persona_scope_allowlist") or {}).items():
        if isinstance(paths, list):
            scope_map[str(persona)] = tuple(str(p) for p in paths)

    cross_cutting: list[frozenset[str]] = []
    for group in raw.get("cross_cutting_groups") or []:
        if isinstance(group, list):
            cross_cutting.append(frozenset(str(p) for p in group))

    worktree_base_raw = raw.get("worktree_base")
    worktree_base = (
        Path(str(worktree_base_raw)).expanduser().resolve() if worktree_base_raw else None
    )

    pr_loop_cfg = _load_pr_loop(repo_root, raw)
    capacity_cfg = _load_capacity(raw)
    from agent_fleet.orchestration.config import resolve_orchestration_config

    target_configs: list[RepoConfig] = []
    if not workspace_raw:
        for entry in raw.get("targets") or []:
            if isinstance(entry, dict) and entry.get("config"):
                target_path = (config_root / str(entry["config"])).resolve()
                target_configs.append(load_repo_config(target_path, controller_root=config_root))

    return RepoConfig(
        repo_root=repo_root,
        config_root=config_root,
        config_path=config_path,
        state_root=state_root,
        target_configs=tuple(target_configs),
        name=str(raw.get("name") or ""),
        default_persona=str(raw.get("default_persona") or "coder"),
        default_branch=str(raw.get("default_branch") or "main"),
        personas_dir=personas_dir,
        skills_dirs=tuple(skills_dirs),
        use_worktree=bool(raw.get("use_worktree", False)),
        worktree_base=worktree_base,
        verify_commands=verify_commands,
        persona_verify_commands=persona_verify_commands,
        worktree_bootstrap_commands=worktree_bootstrap_commands,
        commit_preflight_commands=commit_preflight_commands,
        test_command=test_command,
        lint_command=lint_command,
        typecheck_command=typecheck_command,
        persona_scope_allowlist=scope_map,
        cross_cutting_groups=tuple(cross_cutting),
        critical_path_prefixes=tuple(str(p) for p in (raw.get("critical_path_prefixes") or [])),
        spine_overrides=dict(raw.get("spine") or {}),
        pr_review=load_pr_review_config(repo_root, raw),
        pr_loop=pr_loop_cfg,
        code_review=_load_code_review(raw, pr_loop_cfg),
        issue_dispatch=_load_issue_dispatch(repo_root, raw),
        backlog_dispatcher=_load_backlog_dispatcher(raw),
        schedules=_load_schedules(repo_root, raw),
        capacity=capacity_cfg,
        orchestration=resolve_orchestration_config(raw),
        level_up=load_level_up_config(raw),
    )


def _load_code_review(
    raw: dict[str, Any],
    pr_loop: PrLoopConfig | None,
) -> CodeReviewConfig | None:
    from agent_fleet.code_review.config import resolve_code_review_config

    return resolve_code_review_config(raw, pr_loop=pr_loop)


def _load_pr_loop(repo_root: Path, raw: dict[str, Any]) -> PrLoopConfig | None:
    from agent_fleet.pr_loop.config import load_pr_loop_config

    return load_pr_loop_config(repo_root, raw)


def _load_issue_dispatch(repo_root: Path, raw: dict[str, Any]) -> IssueDispatchConfig | None:
    from agent_fleet.issue_loop.config import load_issue_dispatch_config

    return load_issue_dispatch_config(repo_root, raw)


def _load_backlog_dispatcher(raw: dict[str, Any]) -> BacklogDispatcherConfig | None:
    from agent_fleet.issue_loop.config import load_backlog_dispatcher_config

    return load_backlog_dispatcher_config(raw)


def _load_schedules(repo_root: Path, raw: dict[str, Any]) -> ScheduleConfig | None:
    from agent_fleet.schedule.config import load_schedule_config

    return load_schedule_config(repo_root, raw)


def _load_capacity(raw: dict[str, Any]) -> FleetCapacity:
    from agent_fleet.capacity.config import load_capacity_config

    return load_capacity_config(raw)


def merge_repo_into_fleet_config(
    fleet_config: FleetConfig,
    repo: RepoConfig | None,
) -> FleetConfig:
    """Apply repo-local overrides onto a FleetConfig instance."""
    if repo is None:
        return fleet_config
    if repo.personas_dir and repo.personas_dir.exists():
        fleet_config.personas_dir = repo.personas_dir
    if repo.default_persona:
        fleet_config.default_persona = repo.default_persona
    if repo.capacity is not None:
        fleet_config.max_parallel = repo.capacity.max_dispatches
    from agent_fleet.skills_lib import merge_skill_dirs, repo_skill_dirs

    fleet_config.skill_dirs = merge_skill_dirs(
        fleet_config.skill_dirs,
        repo_skill_dirs(repo),
        list(repo.skills_dirs),
    )
    fleet_config.repo_config = repo
    return fleet_config


def fleet_state_root(repo: RepoConfig) -> Path:
    return repo.state_root or repo.repo_root


def target_registry(repos: list[RepoConfig]) -> dict[Path, RepoConfig]:
    return {target.repo_root.resolve(): target for target in repos}


def iter_target_repos(repo: RepoConfig) -> list[RepoConfig]:
    """Enabled dispatch targets: explicit targets plus self when configured locally."""
    targets = list(repo.target_configs)
    self_root = repo.repo_root.resolve()
    if (
        repo.issue_dispatch is not None
        and repo.issue_dispatch.enabled
        and not any(t.repo_root.resolve() == self_root for t in targets)
    ):
        targets.insert(0, repo)
    if (
        repo.pr_loop is not None
        and repo.pr_loop.enabled
        and not any(t.repo_root.resolve() == self_root for t in targets)
    ):
        targets.insert(0, repo)
    return targets
