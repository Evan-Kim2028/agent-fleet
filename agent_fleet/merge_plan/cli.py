"""``agent-fleet merge`` — execute the batches merge-plan produces.

Registered via :func:`register_merge_commands`, mirroring
``fleet_ops.cli.register_lane_commands``, so ``cli.py`` only gains a two-line
import and call and the argparse surface stays in one place.

The command tree is deliberately separate from the flat ``merge-plan``:
planning is read-only and safe to run at any time, while ``merge run`` merges,
deploys, and touches production.  Keeping them apart means a habit of running
``merge-plan`` never becomes an accidental deploy.
"""

from __future__ import annotations

import contextlib
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping, Sequence

    from agent_fleet.merge_plan.execute import EventSink, TickResult
    from agent_fleet.merge_plan.train import TrainPR
    from agent_fleet.merge_plan.types import ExecutorSpec, RepoSpec


def _spec(args: argparse.Namespace) -> ExecutorSpec:
    """The executor settings, with a malformed block reported not swallowed."""
    from agent_fleet.merge_plan.config import load_executor_spec

    path = getattr(args, "config", None)
    return load_executor_spec(Path(path) if path else None)


def _spec_or_error(args: argparse.Namespace) -> ExecutorSpec | int:
    """The executor settings, or the exit code to report a malformed block.

    Every ``fleet merge`` subcommand reads the same strictly-validated block, so
    the conversion from ``ValueError`` to ``error: ...`` + exit 2 belongs here
    rather than in each command: a subcommand that forgets it prints a Python
    traceback at an operator who only mistyped a key.
    """
    try:
        return _spec(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _make_run_log(run_id: str) -> EventSink | None:
    """A RunLog for this tick, or ``None`` when observability is unavailable.

    Events must never be the reason a merge does not happen, so a failure to
    build the sink degrades to emitting nothing rather than raising.
    """
    try:
        from agent_fleet.observability.log import RunLog

        return RunLog.create(run_id=run_id, include_memory_ring=False)
    except Exception:
        return None


def cmd_merge_run(args: argparse.Namespace) -> int:
    """Run one merge tick, or loop with --daemon."""
    from agent_fleet.merge_plan.execute import run_tick

    config_path = getattr(args, "config", None)
    spec = _spec_or_error(args)
    if isinstance(spec, int):
        return spec

    repo_specs = list(args.repo_path or [])
    if not repo_specs and not _has_configured_repos(config_path):
        print(
            "error: no repos selected. Pass --repo-path PATH (repeatable) or "
            "add merge_plan.repos[] to fleet.yaml.",
            file=sys.stderr,
        )
        return 1

    kwargs: dict[str, Any] = {
        "repo_paths": repo_specs,
        "fleet_config_path": Path(config_path) if config_path else None,
        "spec": spec,
        "operator": None if args.operator in (None, "all") else args.operator,
        "status_dir": Path(args.status_dir).expanduser() if args.status_dir else None,
        "max_batch_size": args.max_batch_size,
        "check_merges": not args.no_merge_check,
        "dry_run": args.dry_run,
    }

    if not args.daemon:
        run_id = f"merge-run-{int(time.time())}"
        result = run_tick(**kwargs, run_log=_make_run_log(run_id), run_id=run_id)
        return _report([result], args.json)

    stop = _stop_flag()
    interval = max(1.0, float(args.daemon))
    results: list[Any] = []
    while not stop():
        run_id = f"merge-run-{int(time.time())}"
        result = run_tick(**kwargs, run_log=_make_run_log(run_id), run_id=run_id)
        # One JSON document per tick, so a daemon stays machine-readable.
        print(
            json.dumps(result.to_dict(), indent=2, default=str)
            if args.json
            else result.render_text(),
            flush=True,
        )
        results.append(result)
        time.sleep(interval)
    return 1 if any(o.status == "failed" for r in results for o in r.outcomes) else 0


def _has_configured_repos(config_path: str | None) -> bool:
    from agent_fleet.merge_plan.config import load_merge_plan_config

    return bool(load_merge_plan_config(Path(config_path) if config_path else None))


def _stop_flag() -> Callable[[], bool]:
    """A closure that becomes true once SIGINT/SIGTERM asks us to stop.

    A daemon left running after the terminal closes is a daemon that keeps
    merging, so the signal sets a flag the loop checks and the loop leaves the
    deploy lock on its own ``finally`` rather than being torn down mid-batch.
    """
    flag = {"stop": False}

    def _handle(signum: int, frame: object) -> None:
        # The handler's contract fixes the signature; the body only sets a flag.
        del signum, frame
        flag["stop"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not on the main thread, or not a signal-capable platform: the daemon
        # then runs until it is stopped some other way.
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sig, _handle)
    return lambda: flag["stop"]


def _report(results: list[TickResult], as_json: bool) -> int:
    """Print the tick(s) and derive an exit code.

    0 when everything that ran succeeded, 1 when a batch failed.  A held or
    locked batch is not a failure: it is the scheduler working, and a cron job
    must not page an operator for it.
    """
    if as_json:
        payload = results[0].to_dict() if len(results) == 1 else [r.to_dict() for r in results]
        print(json.dumps(payload, indent=2, default=str))
    else:
        for result in results:
            print(result.render_text())
    failed = any(o.status == "failed" for r in results for o in r.outcomes)
    return 1 if failed else 0


def cmd_merge_holds(args: argparse.Namespace) -> int:
    """Show which cluster holds are active and which have been released."""
    from agent_fleet.merge_plan.execute import load_ledger

    spec = _spec_or_error(args)
    if isinstance(spec, int):
        return spec
    ledger = load_ledger(spec)
    active = ledger.active_holds(spec)
    released = sorted(ledger.released_holds())
    payload = {
        "active": [h.to_dict() for h in active],
        "released": released,
        "configured": [h.name for h in spec.holds],
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if not active:
        print("no active cluster holds")
    for hold in active:
        match = []
        if hold.lanes:
            match.append(f"lanes {', '.join(hold.lanes)}")
        if hold.deploy_units:
            match.append(f"deploy units {', '.join(hold.deploy_units)}")
        print(f"held: {hold.name}  [{'; '.join(match) or 'matches nothing'}]")
        print(f"  release: fleet merge release {hold.name}")
    if released:
        print(f"released: {', '.join(released)}")
    return 0


def cmd_merge_release(args: argparse.Namespace) -> int:
    """Clear a named cluster hold so its lanes merge again."""
    from agent_fleet.merge_plan.execute import release_hold

    spec = _spec_or_error(args)
    if isinstance(spec, int):
        return spec
    known = {h.name for h in spec.holds}
    if args.hold not in known:
        known_list = ", ".join(sorted(known)) or "(none configured)"
        print(f"error: unknown hold {args.hold!r}; configured holds: {known_list}", file=sys.stderr)
        return 2
    if release_hold(spec, args.hold):
        print(f"released {args.hold}")
        return 0
    print(f"{args.hold} was already released")
    return 0


def cmd_merge_train(args: argparse.Namespace) -> int:
    """Run one merge train: test the approved batch combined, land it once.

    The batch comes from the same gate approvals ``merge run`` reads, and the
    per-PR merge/deploy/verify commands are rendered but *not* executed: a
    landed train is one deploy, and the operator decides that.  ``--dry-run``
    prints what would run and reports nothing as landed.

    Two things are decided *before* the base branch is, and both of them decide
    whether there is a batch at all.  The batch is the first eligible PRs in
    fold order, and the active cluster holds are consulted against it — the same
    ledger and the same matcher ``merge run`` uses, so a freeze declared for
    ``merge run`` is a freeze for the train too.  Resolving the branch over a
    different set than the one that runs would mean asking the batch a question
    it is not the one being asked, which is how a folded-onto and a landed-onto
    branch drift apart.
    """
    from agent_fleet.merge_plan.collect import (
        GitHubClient,
        collect_from_lanes,
        collect_from_status_dir,
        dedupe_approvals,
    )
    from agent_fleet.merge_plan.config import (
        resolve_train_base_branch,
        resolve_train_repo_name,
    )
    from agent_fleet.merge_plan.plan import normalize_repo
    from agent_fleet.merge_plan.train import (
        TrainPR,
        order_batch,
        partition_batch,
        resolve_base_branch,
        run_train,
    )

    repo_path = Path(args.repo_path).expanduser()
    repo = args.repo or resolve_train_repo_name(repo_path)
    if not repo_path.is_dir():
        print(f"error: repo path does not exist: {repo_path}", file=sys.stderr)
        return 2

    approvals = list(collect_from_lanes(operator=args.operator))
    if args.status_dir:
        approvals += collect_from_status_dir(Path(args.status_dir).expanduser(), default_repo=repo)
    # Gate and lane sources record ``owner/name``; the checkout names the bare
    # ``name``.  Without the reconciliation every real approval reads as another
    # repo's and the batch is empty.  Normalising before de-duplicating, as
    # ``build_plan`` does, is what makes one PR one batch entry: de-duplicating
    # first keys on the raw spelling, so the two records of the same PR both
    # survive and it is folded, tested and merged twice.
    approvals = dedupe_approvals([normalize_repo(a, {repo: repo}) for a in approvals])
    approvals = [a for a in approvals if normalize_repo(a, {repo: repo}).repo == repo]
    if not approvals:
        print(f"no approved PRs found for {repo}")
        return 0

    config_path = getattr(args, "config", None)
    lanes = {a.pr_number: a.lane for a in approvals}
    client = GitHubClient().for_repo(repo_path)
    prs: list[TrainPR] = []
    for approval in approvals:
        detail = client.pr_detail(approval.pr_number)
        if detail.get("state") != "OPEN":
            continue
        files = tuple(f.get("path", "") for f in detail.get("files") or [] if isinstance(f, dict))
        prs.append(
            TrainPR(
                number=approval.pr_number,
                head_sha=approval.approved_sha,
                base_ref=str(detail.get("baseRefName") or ""),
                head_branch=str(detail.get("headRefName") or ""),
                current_head=str(detail.get("headRefOid") or ""),
                files=tuple(f for f in files if f),
                head_ref=f"refs/pull/{approval.pr_number}/head",
            )
        )
    if not prs:
        print(f"no open approved PRs found for {repo}")
        return 0

    # One batch, resolved once: the PRs that survive the staleness filter, in
    # fold order, capped.  The cap belongs here rather than inside run_train so
    # the base branch, the hold check and the run are all answered about the same
    # set of PRs.
    keep, moved = partition_batch(prs)
    batch = order_batch(keep)[: args.max_batch_size]

    if args.dry_run:
        print(f"merge train dry-run [{repo}]: would test {len(batch)} PR(s) combined")
        for pr in batch:
            print(f"  #{pr.number} {pr.head_sha[:9]} base={pr.base_ref or '-'}")
        for pr in moved:
            print(f"  #{pr.number} SKIPPED-MOVED head moved to {pr.current_head[:9]}")
        return 0

    held = _held_batch(batch, lanes=lanes, args=args)
    if held is not None:
        print(held, file=sys.stderr)
        return 1

    try:
        base_branch = resolve_base_branch(
            repo_path,
            configured=getattr(args, "base_branch", "")
            or resolve_train_base_branch(repo, Path(config_path) if config_path else None),
            prs=batch,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_train(
            repo=repo,
            repo_path=repo_path,
            prs=batch,
            command=args.test_command,
            max_batch_size=args.max_batch_size,
            report_path=Path(args.report).expanduser() if args.report else None,
            base_branch=base_branch,
        )
    except subprocess.TimeoutExpired as exc:
        # Every git call the fold makes carries a timeout, and only the test
        # command used to handle one.  A hang in the fetch or the worktree add
        # therefore escaped as a raw traceback past a command that turns every
        # other fold failure into an ``error: ...`` sentence, and the candidate
        # directory it had already made stayed behind.
        command = " ".join(str(a) for a in (exc.cmd or ()))
        print(
            f"error: merge train could not fold onto origin/{base_branch}: "
            f"{command} timed out after {exc.timeout}s",
            file=sys.stderr,
        )
        return 2
    except (OSError, RuntimeError) as exc:
        print(
            f"error: merge train could not fold onto origin/{base_branch}: {exc}",
            file=sys.stderr,
        )
        return 2
    # The batch the run was given is the CLI's own, decided above; the trainer
    # narrows it as it folds.  A result that came back without naming its batch
    # would report ``batch_size: 0`` for a train that ran, so the batch is
    # stamped onto a result that carries none.  Never overwritten: a trainer
    # that did fold legitimately drops conflicts and moved heads from ``ordered``.
    if not result.ordered:
        result.ordered = tuple(batch)
    print(
        json.dumps(result.to_dict(), indent=2, default=str) if args.json else result.render_text()
    )
    return 0 if result.landed else 1


def _held_batch(
    batch: Sequence[TrainPR], *, lanes: Mapping[int, str], args: argparse.Namespace
) -> str | None:
    """Why this batch may not be merged under an active cluster hold, or ``None``.

    A hold is an operator saying *nothing* merges for these lanes until they
    release it, and it is held in the ledger rather than in the code, so a
    command that merges without reading it is not subject to the freeze the
    operator is relying on.  A train lands PRs one after another with no other
    checkpoint, so it is the one path that has to ask.

    Matching is ``merge run``'s own: the same active holds, the same per-PR
    ``ClusterHold.matches``, so the set of merges a hold stops is the same set
    whichever command the operator reaches for.  Both halves of that matcher are
    supplied, because a hold is configured on either: the lane the PR is
    recorded under, and the deploy unit its changed files resolve to.  A train
    picks the deploy unit *after* the merges, but each PR already carries the
    files ``gh pr view`` reported and the unit is per-PR
    (``deploy_unit_for``), exactly as ``build_profile`` derives it — so a freeze
    declared over ``deploy_units`` applies here too.  Matching on lane alone made
    ``ClusterHold.matches`` short-circuit on the empty unit and let a
    ``deploy_units``-only freeze be bypassed outright.
    """
    from agent_fleet.merge_plan.config import load_merge_plan_config
    from agent_fleet.merge_plan.execute import load_ledger
    from agent_fleet.merge_plan.profile import deploy_unit_for
    from agent_fleet.merge_plan.train import names_of

    if not batch:
        return None
    try:
        spec = _spec(args)
    except ValueError as exc:
        return f"error: {exc}"
    repo = getattr(args, "repo", "") or ""
    config_path = getattr(args, "config", None)
    repo_specs = load_merge_plan_config(Path(config_path) if config_path else None)
    units = _deploy_units(repo, repo_specs)
    active = load_ledger(spec).active_holds(spec)
    held_by_lane = [
        (hold.name, pr.number)
        for pr in batch
        for hold in active
        if hold.matches(
            lane=lanes.get(pr.number, ""),
            deploy_unit=deploy_unit_for(pr.files, units),
        )
    ]
    if not held_by_lane:
        return None
    name, _first = held_by_lane[0]
    return (
        f"error: cluster hold {name} is holding "
        f"{names_of(tuple(sorted(n for held, n in held_by_lane if held == name)))} "
        f"(release: fleet merge release {name})"
    )


def _deploy_units(repo: str, repo_specs: Mapping[str, RepoSpec]) -> Mapping[str, str]:
    """The ``path prefix -> unit`` table this train's PRs are resolved against.

    A train is over exactly one repository, so the table is that repository's:
    the one named on the command line when the config declares it under a
    different spelling, the config's sole declaration when the command line
    names nothing, and the built-in table for the well-known fleet repos
    otherwise.  Falling back to an empty table is what let a ``deploy_units``
    freeze read as "no unit" and slip past the hold, so every path here resolves
    the real table or the built-in one — never none.
    """
    from agent_fleet.merge_plan.config import builtin_spec

    if repo in repo_specs:
        return repo_specs[repo].deploy_units
    bare = repo.rsplit("/", 1)[-1]
    for name, repo_spec in repo_specs.items():
        if name.rsplit("/", 1)[-1] == bare:
            return repo_spec.deploy_units
    if not repo and len(repo_specs) == 1:
        return next(iter(repo_specs.values())).deploy_units
    return builtin_spec(repo).deploy_units


def register_merge_commands(sub: argparse._SubParsersAction) -> None:
    """Register the ``merge`` command tree on the top-level parser."""
    merge_p = sub.add_parser(
        "merge",
        help="Execute merge-plan batches: merge, deploy, and verify approved PRs",
    )
    merge_sub = merge_p.add_subparsers(dest="merge_command", required=True)

    run_p = merge_sub.add_parser(
        "run",
        help="Run one merge tick (or loop with --daemon)",
    )
    run_p.add_argument(
        "--repo-path",
        action="append",
        help="Checkout to merge for (repeatable). Overrides fleet.yaml merge_plan.repos[]",
    )
    run_p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    run_p.add_argument(
        "--operator",
        default=None,
        help="Only read lanes for this operator (default: all operators)",
    )
    run_p.add_argument(
        "--status-dir",
        help="Directory of gate status files containing PREMERGE-APPROVED <sha> lines",
    )
    run_p.add_argument(
        "--max-batch-size",
        type=int,
        default=5,
        help="Cap on PRs per batch (default 5)",
    )
    run_p.add_argument(
        "--no-merge-check",
        action="store_true",
        help="Skip the git merge-compatibility check when planning",
    )
    run_p.add_argument(
        "--daemon",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Loop, running a tick every SECONDS instead of exiting after one",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would run without executing commands or taking locks",
    )
    run_p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    run_p.set_defaults(func=cmd_merge_run)

    holds_p = merge_sub.add_parser(
        "holds",
        help="Show active and released cluster holds",
    )
    holds_p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    holds_p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    holds_p.set_defaults(func=cmd_merge_holds)

    release_p = merge_sub.add_parser(
        "release",
        help="Release a named cluster hold",
    )
    release_p.add_argument("hold", help="Hold name, as configured in merge_plan.executor.holds")
    release_p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    release_p.set_defaults(func=cmd_merge_release)

    train_p = merge_sub.add_parser(
        "train",
        help="Test the approved batch combined, then land it in one go",
    )
    train_p.add_argument("--repo-path", required=True, help="Checkout to land the batch in")
    train_p.add_argument("--repo", default=None, help="Repo name (default: from --repo-path)")
    train_p.add_argument(
        "--config",
        default=None,
        help="Path to fleet.yaml (default: ~/.agent-fleet/fleet.yaml)",
    )
    train_p.add_argument(
        "--operator",
        default=None,
        help="Only read lanes for this operator (default: all operators)",
    )
    train_p.add_argument(
        "--status-dir",
        help="Directory of gate status files containing PREMERGE-APPROVED <sha> lines",
    )
    train_p.add_argument(
        "--base-branch",
        default=None,
        help="Branch the batch is folded onto (default: the base the PRs name, "
        "else the remote's default branch)",
    )
    train_p.add_argument(
        "--test-command",
        default=None,
        help="Test command for the combined tree; {tree} is the candidate checkout "
        "(default: pytest over the batch's changed test files)",
    )
    train_p.add_argument(
        "--max-batch-size",
        type=int,
        default=5,
        help="Cap on PRs per train (default 5)",
    )
    train_p.add_argument("--report", help="Where to write the JSON report")
    train_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the batch that would be tested without folding, testing, or merging",
    )
    train_p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    train_p.set_defaults(func=cmd_merge_train)
