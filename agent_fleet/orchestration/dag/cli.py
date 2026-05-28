"""CLI handlers for agent-fleet dag subcommands."""

from __future__ import annotations

import argparse  # noqa: TC003 — register_dag_commands uses argparse at runtime
import json
import sys
from pathlib import Path

from agent_fleet.cli_env import require_backend_env
from agent_fleet.config import load_fleet_config
from agent_fleet.dispatcher import FleetDispatcher
from agent_fleet.hooks import FleetTask
from agent_fleet.orchestration.dag.ascii import render_dag_ascii
from agent_fleet.orchestration.dag.canvas_state import initial_run_state
from agent_fleet.orchestration.dag.canvas_writer import DagCanvasWriter
from agent_fleet.orchestration.dag.paths import resolve_canvas_path
from agent_fleet.orchestration.dag.runner import dispatch_dag
from agent_fleet.orchestration.dag.scheduler import topo_sort_ranks, validate_dag_graph
from agent_fleet.orchestration.dag.schema import load_dag_spec
from agent_fleet.personas import YamlPersonaResolver
from agent_fleet.repo import find_repo_config


def _resolve_workspace(args: argparse.Namespace) -> Path:
    return Path(args.workspace or Path.cwd()).resolve()


def _canvas_writer_from_args(args: argparse.Namespace, workspace: Path) -> DagCanvasWriter | None:
    if not getattr(args, "canvas_path", None) and not getattr(args, "canvas", None):
        return None
    path = resolve_canvas_path(
        workspace=workspace,
        canvas_path=getattr(args, "canvas_path", None),
        canvas=getattr(args, "canvas", None),
        canvases_dir=getattr(args, "canvases_dir", None),
    )
    debounce = int(getattr(args, "canvas_debounce_ms", 200))
    return DagCanvasWriter(path, debounce_ms=debounce)


def _print_ascii(
    spec: object,
    ranks: list,
    *,
    status_by_id: dict[str, str] | None = None,
) -> None:
    from agent_fleet.orchestration.dag.schema import DagSpec, DagTask

    assert isinstance(spec, DagSpec)
    typed_ranks: list[list[DagTask]] = ranks
    print(render_dag_ascii(spec, typed_ranks, status_by_id=status_by_id), end="")


def cmd_dag_validate(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    try:
        spec = load_dag_spec(path)
        validate_dag_graph(spec)
        ranks = topo_sort_ranks(spec.tasks)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "valid": True,
            "title": spec.title,
            "task_count": len(spec.tasks),
            "rank_count": len(ranks),
            "ranks": [[task.id for task in rank] for rank in ranks],
        }
        print(json.dumps(payload, indent=2))
    else:
        _print_ascii(spec, ranks)
    return 0


def cmd_dag_run(args: argparse.Namespace) -> int:
    path = Path(args.file).resolve()
    workspace = _resolve_workspace(args)
    config = load_fleet_config(args.config) if args.config else load_fleet_config()

    try:
        spec = load_dag_spec(path)
        validate_dag_graph(spec)
        ranks = topo_sort_ranks(spec.tasks)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    canvas_writer = _canvas_writer_from_args(args, workspace)
    if canvas_writer is not None:
        canvas_writer.schedule(initial_run_state(spec))
        canvas_writer.flush()
        print(f"canvas → {canvas_writer.canvas_path}", file=sys.stderr)
        if args.init_only:
            return 0

    if args.dry_run:
        if not args.json:
            _print_ascii(spec, ranks)
        if args.json:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "title": spec.title,
                        "ranks": [[task.id for task in rank] for rank in ranks],
                    },
                    indent=2,
                )
            )
        return 0

    if (code := require_backend_env(config)) is not None:
        return code

    repo = find_repo_config(workspace)
    if repo and repo.personas_dir:
        config.personas_dir = repo.personas_dir

    resolver = YamlPersonaResolver(config)
    dispatcher = FleetDispatcher(config=config)
    parent = FleetTask(
        goal=spec.title,
        context=args.context or "",
        persona=args.persona or (repo.default_persona if repo else config.default_persona),
        workspace=str(workspace),
        pipeline=args.pipeline or config.default_pipeline,
    )
    orchestration = repo.orchestration if repo and repo.orchestration else None
    default_pipeline = orchestration.default_dag_pipeline if orchestration else "code_review"
    max_chars = orchestration.dag_upstream_context_chars if orchestration else 2000

    summary = dispatch_dag(
        spec=spec,
        parent_task=parent,
        dispatcher=dispatcher,
        persona_resolver=resolver,
        fallback_persona=parent.persona,
        default_pipeline=args.pipeline or default_pipeline,
        max_chars_per_parent=max_chars,
        canvas_writer=canvas_writer,
    )

    if not args.json:
        status_by_id: dict[str, str] = {}
        for result in summary.results:
            node_id = result.goal.rsplit(" — ", 1)[-1]
            status_by_id[node_id] = result.status
        _print_ascii(spec, ranks, status_by_id=status_by_id)
        print()

    print(
        json.dumps(
            {
                "status": summary.aggregate_status,
                "error": summary.error,
                "summary": summary.summary,
                "ranks": summary.ranks,
                "results": [r.__dict__ for r in summary.results],
                **(
                    {"canvas_path": str(canvas_writer.canvas_path)}
                    if canvas_writer is not None
                    else {}
                ),
            },
            indent=2,
            default=str,
        )
    )
    ok = {"completed", "dag_partial"}
    return 0 if summary.aggregate_status in ok else 1


def _add_canvas_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--canvas-path",
        help="Full path to .canvas.tsx (Cursor IDE live view)",
    )
    parser.add_argument(
        "--canvas",
        help="Canvas filename stem (uses ~/.cursor/projects/<repo>/canvases/)",
    )
    parser.add_argument(
        "--canvases-dir",
        help="Override canvases output directory (with --canvas)",
    )
    parser.add_argument(
        "--canvas-debounce-ms",
        type=int,
        default=200,
        help="Debounce canvas writes in milliseconds (default: 200)",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="Write initial PENDING canvas and exit (requires --canvas-path or --canvas)",
    )


def register_dag_commands(sub: argparse._SubParsersAction) -> None:
    dag = sub.add_parser(
        "dag",
        help="Run dependency-graph task batches (cookbook-compatible DAG JSON)",
    )
    dag_sub = dag.add_subparsers(dest="dag_command", required=True)

    validate_p = dag_sub.add_parser("validate", help="Validate DAG JSON and print ranks")
    validate_p.add_argument("--file", required=True, help="Path to DAG JSON file")
    validate_p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of terminal ASCII diagram",
    )
    validate_p.set_defaults(func=cmd_dag_validate)

    run_p = dag_sub.add_parser("run", help="Execute a DAG through the fleet dispatcher")
    run_p.add_argument("--file", required=True, help="Path to DAG JSON file")
    run_p.add_argument("--persona", help="Default persona for nodes without one")
    run_p.add_argument("--pipeline", help="Default pipeline for nodes without one")
    run_p.add_argument("--context", help="Extra parent context for all nodes")
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rank schedule without dispatching",
    )
    run_p.add_argument(
        "--json",
        action="store_true",
        help="Skip ASCII diagram; JSON result only",
    )
    run_p.add_argument("--workspace", help="Repo path (default: cwd)")
    run_p.add_argument("--config", help="Path to fleet.yaml")
    _add_canvas_flags(run_p)
    run_p.set_defaults(func=cmd_dag_run)
