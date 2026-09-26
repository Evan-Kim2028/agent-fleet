"""``fleet post-merge`` — the post-merge hook, as an agent-fleet feature.

Registered via :func:`register_post_merge_commands` so ``cli.py`` gains a
two-line import and call and the argparse surface stays in one place, mirroring
``merge_plan.cli.register_merge_commands``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

    from agent_fleet.post_merge.config import RepoSpec
    from agent_fleet.post_merge.flow import PostMergeResult


def _spec_or_error(args: argparse.Namespace) -> tuple[RepoSpec | None, int | None]:
    """The repo spec, or the exit code to report a malformed config.

    The ``ValueError`` → ``error: ...`` + exit 2 conversion lives here rather
    than in the command body so a mistyped key never reaches an operator as a
    Python traceback.
    """
    from agent_fleet.post_merge.config import resolve_repo_spec

    try:
        return (
            resolve_repo_spec(
                args.repo,
                repo_path=args.repo_path or "",
                fleet_config_path=Path(args.config) if args.config else None,
            ),
            None,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 2


def _parse_prs_or_error(args: argparse.Namespace) -> list[int]:
    """The batch's PR numbers, exiting 2 after reporting a bad ``--prs``."""
    try:
        return parse_pr_list(args.prs)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_post_merge(args: argparse.Namespace) -> int:
    """Run the post-merge flow for a merged batch."""
    from agent_fleet.post_merge.flow import run_post_merge

    spec, err = _spec_or_error(args)
    if err is not None or spec is None:
        return err if err is not None else 2

    if not spec.is_configured:
        print(
            f"error: no post_merge config for repo {args.repo!r}. "
            f"Add post_merge.repos[] with a plan_command to fleet.yaml.",
            file=sys.stderr,
        )
        return 2

    result = run_post_merge(
        spec,
        _parse_prs_or_error(args),
        deploy_rc=args.deploy_rc,
        main_sha=args.main_sha or "",
        repo_path=args.repo_path or "",
        write_notes=not args.no_handoff,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        _report(result)

    if not result.ok:
        return 1
    return 1 if any(j.status == "failed" for j in result.jobs) else 0


def _report(result: PostMergeResult) -> None:
    """Human-readable summary of one run."""
    print(f"post-merge {result.repo}: {len(result.plans)} planned, {result.cached_plans} cached")
    for planned in result.plans:
        labels = ", ".join(planned.plan.labels()) or "(none)"
        marker = "cached" if planned.cached else "planned"
        print(f"  #{planned.pr_number} [{marker}] {labels}")
    for pr, (add, remove) in result.label_changes.items():
        if add or remove:
            parts = []
            if add:
                parts.append(f"+{', '.join(add)}")
            if remove:
                parts.append(f"-{', '.join(remove)}")
            print(f"  labels #{pr}: {'  '.join(parts)}")
    if result.jobs:
        print("  jobs:")
        for job in result.jobs:
            detail = f" — {job.detail}" if job.detail else ""
            print(f"    {job.job} [{job.slot}]: {job.status}{detail}")
    if result.note_path:
        print(f"  handoff: {result.note_path}")
    for error in result.errors:
        print(f"  error: {error}", file=sys.stderr)


def register_post_merge_commands(sub: argparse._SubParsersAction) -> None:
    """Register the ``post-merge`` command on the top-level parser."""
    p = sub.add_parser(
        "post-merge",
        help="Plan, label, trigger and hand off a merged batch (per repo, via fleet.yaml)",
    )
    p.add_argument("--repo", required=True, help="Repo name as configured in post_merge.repos[]")
    p.add_argument(
        "--prs",
        required=True,
        metavar="N,N",
        help="Comma-separated merged PR numbers for this batch",
    )
    p.add_argument(
        "--deploy-rc",
        type=int,
        default=0,
        help="Deploy exit code. Jobs are queued only when this is 0 (default 0)",
    )
    p.add_argument(
        "--repo-path",
        default=None,
        help="Checkout of the repo (overrides fleet.yaml post_merge.repos[].path)",
    )
    p.add_argument(
        "--main-sha",
        default=None,
        help="Main head sha after the merge, recorded in the hand-off note",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    p.add_argument(
        "--no-handoff",
        action="store_true",
        help="Skip writing the hand-off note and INDEX line",
    )
    p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    p.set_defaults(func=cmd_post_merge, _pr_numbers=None)


def parse_pr_list(raw: str) -> list[int]:
    """``"12,13"`` → ``[12, 13]``. A non-numeric entry is a usage error."""
    numbers: list[int] = []
    for part in str(raw).split(","):
        token = part.strip()
        if not token:
            continue
        try:
            numbers.append(int(token))
        except ValueError as exc:
            raise ValueError(f"--prs expects comma-separated PR numbers, got {part!r}") from exc
    if not numbers:
        raise ValueError("--prs needs at least one PR number")
    return numbers
