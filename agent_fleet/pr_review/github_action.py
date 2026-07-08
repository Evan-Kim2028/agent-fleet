"""GitHub Actions entrypoint for Agent Fleet PR analyzer.

Backend selection follows the fleet config (``default_backend`` in
``AGENT_FLEET_CONFIG`` / ``CODING_FLEET_CONFIG`` / ``~/.agent-fleet/fleet.yaml``),
the same path as ``fleet run``. Optional env overrides are applied inside
``load_fleet_config`` for every entry point:

- ``AGENT_FLEET_BACKEND`` — replace ``default_backend`` when set
- ``AGENT_FLEET_MODEL`` — replace ``default_model`` when set

Auth uses ``require_backend_env`` (env keys and Grok ``auth_probe``).
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.backends import make_backend
from agent_fleet.cli_env import require_backend_env
from agent_fleet.config import load_fleet_config
from agent_fleet.pr_review.analyzer import analyze_changes
from agent_fleet.pr_review.config import PrReviewConfig, load_pr_review_config
from agent_fleet.pr_review.format import format_comment
from agent_fleet.pr_review.git import (
    get_changed_files,
    get_diff,
    is_oversized_pr,
    is_trivial_pr,
)
from agent_fleet.pr_review.github import (
    load_github_event,
    resolve_pr_from_event,
    upsert_pr_comment,
)
from agent_fleet.repo import find_repo_config

if TYPE_CHECKING:
    from agent_fleet.config import FleetConfig

# Stock title from PrReviewConfig — when still this value, rewrite to a
# backend-specific title so comments reflect the runner backend.
_STOCK_COMMENT_TITLE = "Composer PR Analysis"
_STOCK_BACKEND_LABEL = "Agent Fleet"

# backend → (comment title, footer product label)
_BACKEND_DISPLAY: dict[str, tuple[str, str]] = {
    "cursor": ("Composer PR Analysis", "Composer"),
    "grok": ("Grok PR Analysis", "Grok Build"),
    "kimi": ("Kimi PR Analysis", "Kimi Code"),
    "openrouter": ("OpenRouter PR Analysis", "OpenRouter"),
}


def backend_display(backend: str) -> tuple[str, str]:
    """Return (comment_title, footer_label) for a backend name."""
    return _BACKEND_DISPLAY.get(
        backend.lower().strip(),
        ("Agent Fleet PR Analysis", "Agent Fleet"),
    )


def resolve_comment_title(pr_config: PrReviewConfig, backend: str) -> str:
    """Prefer a customized pr_review.comment_title; else backend-specific title."""
    configured = (pr_config.comment_title or "").strip()
    if configured and configured != _STOCK_COMMENT_TITLE:
        return configured
    title, _ = backend_display(backend)
    return title


def resolve_footer_label(pr_config: PrReviewConfig, backend: str) -> str:
    """Prefer a customized pr_review.backend_label; else backend product name."""
    configured = (pr_config.backend_label or "").strip()
    if configured and configured != _STOCK_BACKEND_LABEL:
        return configured
    _, label = backend_display(backend)
    return label


def resolve_fleet_config() -> FleetConfig:
    """Load fleet.yaml (env AGENT_FLEET_BACKEND / AGENT_FLEET_MODEL applied in loader)."""
    return load_fleet_config()


def main() -> int:
    from agent_fleet.telemetry import configure_fleet_logging

    configure_fleet_logging()
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("Error: GITHUB_TOKEN and GITHUB_REPOSITORY required", file=sys.stderr)
        return 1

    fleet_config = resolve_fleet_config()
    if (code := require_backend_env(fleet_config)) is not None:
        return code

    backend_name = fleet_config.default_backend.lower()
    backend = make_backend(fleet_config)

    event = load_github_event()
    pull_request = resolve_pr_from_event(event, repo, token)
    pr_number = int(pull_request["number"])
    base_sha = str(pull_request["base"]["sha"])
    head_sha = str(pull_request["head"]["sha"])

    workspace = Path(os.environ.get("AGENT_FLEET_WORKSPACE", Path.cwd())).resolve()
    repo_config = find_repo_config(workspace)
    raw: dict = {}
    if repo_config:
        for name in (".agent-fleet.yaml", ".agent-fleet.yml"):
            path = repo_config.repo_root / name
            if path.exists():
                import yaml

                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(loaded, dict):
                    raw = loaded
                break
    pr_config = load_pr_review_config(repo_config.repo_root if repo_config else workspace, raw)
    if pr_config is None:
        pr_config = PrReviewConfig()

    comment_title = resolve_comment_title(pr_config, backend_name)
    footer_label = resolve_footer_label(pr_config, backend_name)
    model_label = fleet_config.default_model or backend_name
    marker = comment_title

    cwd = repo_config.repo_root if repo_config else workspace
    files = get_changed_files(base_sha, head_sha, cwd=cwd)
    diff = get_diff(base_sha, head_sha, cwd=cwd)

    if is_trivial_pr(files, pr_config.trivial_patterns):
        body = textwrap.dedent(f"""\
            ## 🤖 {marker}

            Skipped — all changed files are docs, locks, or assets only.
        """)
        upsert_pr_comment(
            repo=repo,
            pr_number=pr_number,
            token=token,
            body=body,
            marker=marker,
        )
        return 0

    if is_oversized_pr(files, pr_config.oversized_threshold):
        body = textwrap.dedent(f"""\
            ## 🤖 {marker}

            Skipped — {len(files)} files changed (threshold {pr_config.oversized_threshold}).
            Split the PR or exclude generated/venv artifacts.
        """)
        upsert_pr_comment(
            repo=repo,
            pr_number=pr_number,
            token=token,
            body=body,
            marker=marker,
        )
        return 0

    analysis = analyze_changes(
        diff=diff,
        files=files,
        config=pr_config,
        backend=backend,
        workspace=cwd,
        pr_number=pr_number,
        model=fleet_config.default_model,
        timeout_s=fleet_config.timeout_seconds,
    )
    body = format_comment(
        analysis,
        title=comment_title,
        footer=f"Generated by {footer_label} ({model_label})",
    )
    upsert_pr_comment(
        repo=repo,
        pr_number=pr_number,
        token=token,
        body=body,
        marker=marker,
    )
    print(f"Posted analysis for PR #{pr_number} via {backend_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
