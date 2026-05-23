"""Scope fleet-dispatchable work using thermo-nuclear quality review standards."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.backends import make_backend
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.repo import RepoConfig, find_repo_config, merge_repo_into_fleet_config
from agent_fleet.skills_lib import DEFAULT_QUALITY_REVIEW_SKILL, load_skill_text

if TYPE_CHECKING:
    from agent_fleet.hooks import LLMBackend

_SCOPE_JSON = textwrap.dedent("""\
    Return strict JSON:
    {
      "repo": "name",
      "summary": "1-2 sentence overview",
      "ranked_tasks": [
        {
          "rank": 1,
          "title": "short title",
          "source": "github_issue|code_smell|test_gap|refactor",
          "issue_number": null,
          "persona": "backend|frontend|data|lakestore|coder",
          "pipeline": "code_review|simple",
          "workspace_paths": ["paths/to/touch"],
          "complexity": "S|M|L",
          "goal": "dispatch-ready goal for coding_fleet_dispatch",
          "context": "constraints, verify commands, scope limits",
          "quality_risks": ["thermo-nuclear concerns if any"],
          "blocked_by_critical_path": false
        }
      ]
    }
""")


def _gh_issues(repo: str, *, limit: int = 20) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,labels,body",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _large_source_files(repo_root: Path, *, min_lines: int = 800, limit: int = 15) -> list[str]:
    extensions = {".py", ".ts", ".tsx", ".js", ".jsx"}
    skip_dirs = {
        "node_modules",
        ".git",
        "venv",
        ".venv",
        "dist",
        "build",
        "__pycache__",
        ".worktrees",
    }
    sizes: list[tuple[int, str]] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in extensions:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            line_count = sum(1 for _ in path.open(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if line_count >= min_lines:
            sizes.append((line_count, str(path.relative_to(repo_root))))
    sizes.sort(reverse=True)
    return [rel for _, rel in sizes[:limit]]


def _repo_github_slug(repo: RepoConfig) -> str | None:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo.repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    if "github.com" not in url:
        return None
    slug = url.rstrip("/").removesuffix(".git")
    if slug.startswith("git@"):
        slug = slug.split(":", 1)[-1]
    else:
        slug = slug.split("github.com/", 1)[-1]
    return slug or None


def build_scope_prompt(
    *,
    repo: RepoConfig,
    issues: list[dict[str, Any]],
    large_files: list[str],
    skill_text: str,
) -> str:
    allowlist = json.dumps(repo.persona_scope_allowlist, indent=2)
    critical = ", ".join(repo.critical_path_prefixes) or "(none)"
    issues_block = json.dumps(
        [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "labels": [label.get("name") for label in item.get("labels") or []],
            }
            for item in issues
        ],
        indent=2,
    )
    large_block = "\n".join(f"- {path}" for path in large_files) or "- (none over 800 lines)"

    return textwrap.dedent(f"""\
        You are scoping work for an agent coding fleet dispatch.

        Apply the thermo-nuclear code quality review lens when ranking tasks:
        prefer decompositions, boundary fixes, and code-judo refactors over
        feature sprawl. Avoid tasks that require editing critical paths unless
        explicitly necessary.

        ## Thermo-nuclear quality standards
        {skill_text}

        ## Repository
        - name: {repo.display_name}
        - root: {repo.repo_root}
        - default_persona: {repo.default_persona}
        - critical_path_prefixes: {critical}
        - persona_scope_allowlist:
        {allowlist}

        ## Open GitHub issues
        {issues_block}

        ## Large source files (>=800 lines — decomposition candidates)
        {large_block}

        ## Instructions
        1. Rank 5-10 fleet-dispatchable tasks (S/M/L complexity).
        2. Each task must have a concrete `goal` and `context` for coding_fleet_dispatch.
        3. Map persona to allowlist keys when possible.
        4. Mark `blocked_by_critical_path: true` if task touches protected prefixes.
        5. Flag thermo-nuclear risks (1k+ line files, spaghetti, wrong layer).

        {_SCOPE_JSON}
    """)


def run_scope(
    *,
    workspace: Path,
    fleet_config: FleetConfig | None = None,
    backend: LLMBackend | None = None,
    github_repo: str | None = None,
    issue_limit: int = 20,
    skill_name: str = DEFAULT_QUALITY_REVIEW_SKILL,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    repo = find_repo_config(workspace)
    if repo is None:
        raise RuntimeError(f"No .agent-fleet.yaml under {workspace}")

    config = merge_repo_into_fleet_config(fleet_config or load_fleet_config(), repo)
    engine = backend or make_backend(config)
    skill_text = load_skill_text(skill_name, list(config.skill_dirs))

    slug = github_repo or _repo_github_slug(repo)
    issues = _gh_issues(slug, limit=issue_limit) if slug else []
    large_files = _large_source_files(repo.repo_root)

    prompt = build_scope_prompt(
        repo=repo,
        issues=issues,
        large_files=large_files,
        skill_text=skill_text,
    )
    result = engine.run(
        prompt,
        max_tokens=8192,
        timeout_s=config.timeout_seconds,
        cwd=repo.repo_root,
        model=config.default_model,
        mode="plan",
        allowed_tools=["Read", "Grep"],
    )
    if result.exit_code != 0:
        return {
            "error": result.stderr or "scope agent failed",
            "stdout": result.stdout,
            "issues_count": len(issues),
            "large_files": large_files,
        }

    text = result.stdout.strip()
    start = text.find("{")
    if start == -1:
        return {"error": "no JSON in scope output", "raw": text}

    depth = 0
    for index, char in enumerate(text[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parsed = json.loads(text[start : index + 1])
                parsed["issues_count"] = len(issues)
                parsed["large_files"] = large_files
                return parsed
    return {"error": "unterminated JSON in scope output", "raw": text}
