"""Fleet Scouts — product + technical intake (read-only)."""

from __future__ import annotations

import json
import subprocess
import textwrap
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.backends import make_backend
from agent_fleet.config import FleetConfig, load_fleet_config
from agent_fleet.repo import RepoConfig, find_repo_config, merge_repo_into_fleet_config

if TYPE_CHECKING:
    from agent_fleet.hooks import LLMBackend

_PRODUCT_JSON = textwrap.dedent("""\
    {
      "problem_statement": "string",
      "target_users": ["string"],
      "jobs_to_be_done": ["string"],
      "proposed_epics": [
        {
          "title": "string",
          "user_value": "string",
          "success_metrics": ["string"],
          "open_questions": ["string"],
          "priority": "P0|P1|P2"
        }
      ],
      "out_of_scope": ["string"],
      "assumptions": ["string"],
      "confidence": "low|medium|high"
    }
""")

_TECH_SHARD_JSON = textwrap.dedent("""\
    {
      "scope_prefix": "string",
      "summary": "string",
      "packages": [
        {
          "path": "string",
          "role": "string",
          "entrypoints": ["string"],
          "test_command": "string or empty",
          "risk_zones": ["string"],
          "extension_points": ["string"]
        }
      ],
      "conventions": ["string"],
      "confidence": "low|medium|high"
    }
""")

_SCOUT_BRIEF_JSON = textwrap.dedent("""\
    {
      "repo": "string",
      "summary": "1-2 sentences",
      "product": { ...ProductBrief... },
      "technical": {
        "summary": "string",
        "packages": [],
        "cross_cutting_boundaries": [["path/", "other/"]],
        "large_files": ["string"],
        "conventions": ["string"],
        "confidence": "low|medium|high"
      },
      "recommended_next_moves": [
        {
          "rank": 1,
          "type": "epic|task|spike|defer",
          "title": "string",
          "rationale": "string",
          "persona": "coder|backend|frontend",
          "pipeline": "code_review|simple",
          "workspace_paths": ["string"],
          "blocked_by_critical_path": false
        }
      ],
      "scout_run_id": "string",
      "generated_at": "ISO8601"
    }
""")


def _parse_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in scout output")
    depth = 0
    for index, char in enumerate(text[start:], start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                parsed = json.loads(text[start : index + 1])
                if isinstance(parsed, dict):
                    return parsed
                raise ValueError("scout JSON root must be an object")
    raise ValueError("unterminated JSON in scout output")


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
    if not url:
        return None
    slug = url.rstrip("/").removesuffix(".git")
    slug = slug.split(":", 1)[-1] if slug.startswith("git@") else slug.split("github.com/", 1)[-1]
    return slug or None


def _read_product_context(repo: RepoConfig) -> str:
    candidates = [
        repo.repo_root / "agents" / "product_context.md",
        repo.repo_root / "docs" / "PRODUCT.md",
        repo.repo_root / "README.md",
    ]
    parts: list[str] = []
    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8")[:8000]
            rel = path.relative_to(repo.repo_root)
            parts.append(f"### {rel}\n{text}")
    return "\n\n".join(parts) or "(no product docs found)"


def _tech_scope_prefixes(repo: RepoConfig) -> list[str]:
    prefixes: set[str] = set()
    for paths in repo.persona_scope_allowlist.values():
        for path in paths:
            top = path.split("/")[0]
            if top:
                prefixes.add(f"{top}/")
    if not prefixes:
        for entry in repo.repo_root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                prefixes.add(f"{entry.name}/")
    return sorted(prefixes)[:8]


def _large_source_files(repo_root: Path, *, min_lines: int = 800, limit: int = 10) -> list[str]:
    extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}
    skip = {".git", "node_modules", ".venv", "venv", "dist", "build"}
    found: list[tuple[int, str]] = []
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix not in extensions:
            continue
        if any(part in skip for part in path.parts):
            continue
        try:
            lines = sum(1 for _ in path.open(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if lines >= min_lines:
            found.append((lines, str(path.relative_to(repo_root))))
    found.sort(reverse=True)
    return [path for _lines, path in found[:limit]]


def _run_scout_prompt(
    *,
    backend: LLMBackend,
    prompt: str,
    repo_root: Path,
    fleet_config: FleetConfig,
) -> dict[str, Any]:
    result = backend.run(
        prompt,
        max_tokens=8192,
        timeout_s=fleet_config.timeout_seconds,
        cwd=repo_root,
        model=fleet_config.default_model,
        mode="plan",
        allowed_tools=["Read", "Grep"],
    )
    if result.exit_code != 0:
        return {"error": result.stderr or "scout agent failed", "stdout": result.stdout}
    try:
        return _parse_json_object(result.stdout)
    except ValueError as exc:
        return {"error": str(exc), "raw": result.stdout}


def run_product_scout(
    *,
    repo: RepoConfig,
    backend: LLMBackend,
    fleet_config: FleetConfig,
    github_repo: str | None,
    issue_limit: int,
    product_context: str,
) -> dict[str, Any]:
    slug = github_repo or _repo_github_slug(repo)
    issues = _gh_issues(slug, limit=issue_limit) if slug else []
    docs = _read_product_context(repo)
    extra = product_context.strip()
    prompt = textwrap.dedent(f"""\
        You are the product scout for fleet intake. Read-only — no file edits.

        ## Repository
        - name: {repo.display_name}
        - root: {repo.repo_root}

        ## Product docs
        {docs}

        ## Additional context
        {extra or "(none)"}

        ## Open GitHub issues
        {json.dumps(issues, indent=2) if issues else "[]"}

        Return strict JSON matching:
        {_PRODUCT_JSON}
    """)
    return _run_scout_prompt(
        backend=backend,
        prompt=prompt,
        repo_root=repo.repo_root,
        fleet_config=fleet_config,
    )


def run_tech_scout_shard(
    *,
    repo: RepoConfig,
    prefix: str,
    backend: LLMBackend,
    fleet_config: FleetConfig,
) -> dict[str, Any]:
    critical = ", ".join(repo.critical_path_prefixes) or "(none)"
    prompt = textwrap.dedent(f"""\
        You are a technical scout shard. Scope: `{prefix}` only. Read-only.

        ## Repository
        - name: {repo.display_name}
        - critical_path_prefixes: {critical}

        Map architecture under `{prefix}`: entrypoints, tests, risks, extension points.

        Return strict JSON matching:
        {_TECH_SHARD_JSON}

        Set scope_prefix to "{prefix}".
    """)
    return _run_scout_prompt(
        backend=backend,
        prompt=prompt,
        repo_root=repo.repo_root,
        fleet_config=fleet_config,
    )


def run_tech_scout(
    *,
    repo: RepoConfig,
    backend: LLMBackend,
    fleet_config: FleetConfig,
    max_workers: int,
) -> dict[str, Any]:
    prefixes = _tech_scope_prefixes(repo)
    if not prefixes:
        prefixes = ["."]

    def _shard(prefix: str) -> dict[str, Any]:
        return run_tech_scout_shard(
            repo=repo,
            prefix=prefix,
            backend=backend,
            fleet_config=fleet_config,
        )

    with ThreadPoolExecutor(max_workers=min(max_workers, len(prefixes))) as pool:
        shards = list(pool.map(_shard, prefixes))

    large_files = _large_source_files(repo.repo_root)
    if repo.cross_cutting_groups:
        groups = [[sorted(a)[0], sorted(b)[0]] for a, b in repo.cross_cutting_groups[:6]]
    else:
        groups = []

    packages: list[dict[str, Any]] = []
    conventions: list[str] = []
    for shard in shards:
        if "error" in shard:
            continue
        packages.extend(shard.get("packages") or [])
        conventions.extend(shard.get("conventions") or [])

    return {
        "summary": f"Mapped {len(prefixes)} scope prefix(es) under {repo.display_name}.",
        "packages": packages,
        "cross_cutting_boundaries": groups,
        "large_files": large_files,
        "conventions": sorted(set(conventions)),
        "confidence": "medium",
        "shards": shards,
    }


def synthesize_scout_brief(
    *,
    repo: RepoConfig,
    product: dict[str, Any],
    technical: dict[str, Any],
    backend: LLMBackend,
    fleet_config: FleetConfig,
) -> dict[str, Any]:
    allowlist = json.dumps(
        {key: list(paths) for key, paths in repo.persona_scope_allowlist.items()},
        indent=2,
    )
    prompt = textwrap.dedent(f"""\
        Synthesize a Fleet Scout Brief from product + technical scout outputs.
        Read-only reasoning — return JSON only.

        ## Product scout output
        {json.dumps(product, indent=2)}

        ## Technical scout output
        {json.dumps(technical, indent=2)}

        ## Persona scope allowlist
        {allowlist}

        Produce ranked recommended_next_moves (5-8) ready for coding_fleet_dispatch.
        Merge technical.packages into technical section. Use repo name "{repo.display_name}".

        Return strict JSON matching:
        {_SCOUT_BRIEF_JSON}
    """)
    parsed = _run_scout_prompt(
        backend=backend,
        prompt=prompt,
        repo_root=repo.repo_root,
        fleet_config=fleet_config,
    )
    if "error" not in parsed:
        parsed.setdefault("scout_run_id", uuid.uuid4().hex[:12])
        parsed.setdefault("generated_at", datetime.now(tz=UTC).isoformat())
        parsed.setdefault("repo", repo.display_name)
    return parsed


def run_scout(
    *,
    workspace: Path,
    fleet_config: FleetConfig | None = None,
    backend: LLMBackend | None = None,
    github_repo: str | None = None,
    issue_limit: int = 20,
    product_context: str = "",
    depth: str = "light",
) -> dict[str, Any]:
    """Run Fleet Scouts (light default: product + tech parallel → synthesize)."""
    workspace = workspace.resolve()
    repo = find_repo_config(workspace)
    if repo is None:
        raise RuntimeError(f"No .agent-fleet.yaml under {workspace}")

    config = merge_repo_into_fleet_config(fleet_config or load_fleet_config(), repo)
    engine = backend or make_backend(config)
    del depth  # deep mode reserved for follow-up research rounds

    product = run_product_scout(
        repo=repo,
        backend=engine,
        fleet_config=config,
        github_repo=github_repo,
        issue_limit=issue_limit,
        product_context=product_context,
    )
    technical = run_tech_scout(
        repo=repo,
        backend=engine,
        fleet_config=config,
        max_workers=config.max_parallel,
    )
    brief = synthesize_scout_brief(
        repo=repo,
        product=product,
        technical=technical,
        backend=engine,
        fleet_config=config,
    )
    if "error" in brief:
        brief["product"] = product
        brief["technical"] = technical
    return brief
