"""Agent dispatch — thin entrypoint for agent-fleet issue dispatch."""

import os
import re
import subprocess
import sys
from pathlib import Path

import agents.github as ghmod
from agents.constants import GH_SUBPROCESS_TIMEOUT_S, PERSONA_PATTERN


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: Required env var {name!r} not set.", file=sys.stderr)
        sys.exit(1)
    return val


def get_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent,
        check=True,
    )
    return Path(result.stdout.strip())


def load_env() -> dict:
    dotenv = get_repo_root() / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    repo_full_name = os.environ.get("REPO_FULL_NAME")
    if not repo_full_name:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GH_SUBPROCESS_TIMEOUT_S,
        )
        repo_full_name = result.stdout.strip() if result.returncode == 0 else ""
    if not repo_full_name:
        print("ERROR: REPO_FULL_NAME not set and gh repo view failed.", file=sys.stderr)
        sys.exit(1)

    return {
        "comment_body": _require_env("COMMENT_BODY"),
        "issue_number": int(_require_env("ISSUE_NUMBER")),
        "repo_full_name": repo_full_name,
        "kimi_api_key": _require_env("KIMI_API_KEY"),
        "comment_id": os.environ.get("COMMENT_ID", ""),
    }


def parse_persona(comment_body: str) -> str | None:
    match = re.search(PERSONA_PATTERN, comment_body)
    return match.group(1).strip() if match else None


def _fleet_enabled() -> bool:
    """Return True when issue dispatch is enabled in fleet config.

    Reads issue_dispatch.enabled from repo-root .agent-fleet.yaml.
    Defaults to False on any error (fail-closed).
    """
    repo_root = Path(__file__).resolve().parents[2]

    try:
        from agent_fleet.repo import find_repo_config

        repo = find_repo_config(repo_root)
        if repo is not None and repo.issue_dispatch is not None:
            return bool(repo.issue_dispatch.enabled)
    except Exception:
        pass

    try:
        import yaml

        config_path = repo_root / ".agent-fleet.yaml"
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                section = raw.get("issue_dispatch") or {}
                if isinstance(section, dict) and "enabled" in section:
                    return bool(section["enabled"])
    except Exception:
        pass

    return False


def main() -> None:
    if not _fleet_enabled():
        print(
            "ERROR: issue_dispatch is not enabled. "
            "Set issue_dispatch.enabled: true in .agent-fleet.yaml — "
            "legacy Kimi dispatch was removed.",
            file=sys.stderr,
        )
        sys.exit(1)

    env = load_env()
    ghmod.set_repo(env["repo_full_name"])

    from agent_fleet.issue_loop.dispatch import run_issue_dispatch

    repo_root = Path(__file__).resolve().parents[2]
    persona = parse_persona(env["comment_body"]) or "backend"
    code = run_issue_dispatch(
        issue_number=env["issue_number"],
        comment_body=env["comment_body"],
        repo_root=repo_root,
        persona=persona,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
