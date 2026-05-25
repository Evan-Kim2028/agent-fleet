"""Per-repository configuration discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from agent_fleet.pr_review.config import PrReviewConfig, load_pr_review_config

if TYPE_CHECKING:
    from agent_fleet.capacity.config import FleetCapacity
    from agent_fleet.code_review.config import CodeReviewConfig
    from agent_fleet.config import FleetConfig
    from agent_fleet.issue_loop.config import IssueDispatchConfig
    from agent_fleet.orchestration.config import OrchestrationConfig
    from agent_fleet.pr_loop.config import PrLoopConfig

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
    name: str = ""
    default_persona: str = "coder"
    default_branch: str = "main"
    personas_dir: Path | None = None
    skills_dirs: tuple[Path, ...] = ()
    use_worktree: bool = False
    worktree_base: Path | None = None
    verify_commands: list[str] = field(default_factory=list)
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
    capacity: FleetCapacity | None = None
    orchestration: OrchestrationConfig | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.repo_root.name


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


def load_repo_config(path: Path | str) -> RepoConfig:
    config_path = Path(path).expanduser().resolve()
    repo_root = config_path.parent
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}

    verify_commands = list(raw.get("verify_commands") or [])
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

    return RepoConfig(
        repo_root=repo_root,
        name=str(raw.get("name") or ""),
        default_persona=str(raw.get("default_persona") or "coder"),
        default_branch=str(raw.get("default_branch") or "main"),
        personas_dir=personas_dir,
        skills_dirs=tuple(skills_dirs),
        use_worktree=bool(raw.get("use_worktree", False)),
        worktree_base=worktree_base,
        verify_commands=verify_commands,
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
        capacity=capacity_cfg,
        orchestration=resolve_orchestration_config(raw),
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
