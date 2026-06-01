"""Console entry point: ``python -m silphco.selfimprove``.

Runs one nightly self-improvement pass and exits with code 0 on success (or
no-op), 1 on unrecoverable error.

Usage::

    # Run one nightly pass (uses KimiBackend + GHForge from environment)
    python -m silphco.selfimprove

    # Dry run (no git operations, no PRs opened)
    python -m silphco.selfimprove --dry-run

    # Override the log path
    python -m silphco.selfimprove --log-path /path/to/run_log.jsonl

    # Adjust the mining window and threshold
    python -m silphco.selfimprove --days 14 --min-occurrences 3

Full option reference::

    python -m silphco.selfimprove --help
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m silphco.selfimprove",
        description="Nightly self-improvement loop for the SilphCo Agent Fleet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=None,
        help=(
            "Path to the run log NDJSON file or directory. "
            "Defaults to <repo_root>/data/state/run_log.jsonl."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look-back window in calendar days (default: 30).",
    )
    parser.add_argument(
        "--min-occurrences",
        type=int,
        default=5,
        help="Minimum failure count to be actionable (default: 5).",
    )
    parser.add_argument(
        "--max-prs",
        type=int,
        default=2,
        help="Maximum PRs to open per run (default: 2).",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        help="Git branch to branch from (default: main).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Repository root. When absent, resolved automatically from the "
            "current working directory via git rev-parse."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip git and PR operations; useful for debugging.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def _find_repo_root() -> Path:
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log = logging.getLogger("silphco.selfimprove")

    repo_root = args.repo_root or _find_repo_root()
    log.info("Repository root: %s", repo_root)

    # Late imports so the module can be imported cheaply in tests without
    # pulling in heavy dependencies.
    from silphco.selfimprove.gate import GatePreconditionError
    from silphco.selfimprove.loop import run_loop

    try:
        from agent_fleet.kimi_backend import KimiBackend
        backend = KimiBackend()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to initialise KimiBackend: %s", exc)
        return 1

    try:
        from agent_fleet.integrations.github_forge import GitHubForge
        forge = GitHubForge(cwd=repo_root)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to initialise GitHubForge: %s", exc)
        return 1

    try:
        result = run_loop(
            repo_root=repo_root,
            backend=backend,
            forge=forge,
            log_path=args.log_path,
            days=args.days,
            min_occurrences=args.min_occurrences,
            base_branch=args.base_branch,
            max_prs=args.max_prs,
            dry_run=args.dry_run,
        )
    except GatePreconditionError as exc:
        log.error("Gate precondition error: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error in self-improvement loop: %s", exc)
        return 1

    if result.skipped_reason:
        log.info("Loop completed (no-op): %s", result.skipped_reason)
    else:
        log.info(
            "Loop completed: %d PRs opened, %d rejected, %d attempted.",
            len(result.prs_opened),
            result.proposals_rejected,
            result.proposals_attempted,
        )
        if result.prs_opened:
            log.info("PR numbers: %s", result.prs_opened)

    return 0


if __name__ == "__main__":
    sys.exit(main())
