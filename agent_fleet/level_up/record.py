"""Shared experience recording for dispatcher and LocalFleetRunner paths."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from agent_fleet.level_up.experience import append_experience, compute_experience_weight
from agent_fleet.level_up.journal import append_journal
from agent_fleet.level_up.paths import FLEET_TIER
from agent_fleet.level_up.paths import repo_key as level_up_repo_key
from agent_fleet.observability.context import get_run_log
from agent_fleet.observability.run_metrics import build_run_metrics
from agent_fleet.repo import find_repo_config

if TYPE_CHECKING:
    from pathlib import Path

    from agent_fleet.config import FleetConfig
    from agent_fleet.hooks import FleetTask
    from agent_fleet.level_up.models import DispatchEquip
    from agent_fleet.repo import RepoConfig
    from agent_fleet.runner import FleetRunResult


def equip_snapshot_from_dispatch(equip: DispatchEquip | None) -> dict[str, Any]:
    if equip is None:
        return {}
    snapshot = asdict(equip)
    snapshot["skill_slots_execute"] = list(equip.skill_slots_execute)
    snapshot["skill_slots_review"] = list(equip.skill_slots_review)
    return snapshot


def parse_experience_source_context(context: str) -> tuple[str, int | None]:
    source = "cli"
    pr_loop_round: int | None = None
    ctx = context.strip()
    if not ctx:
        return source, pr_loop_round
    try:
        parsed = json.loads(ctx)
    except json.JSONDecodeError:
        return source, pr_loop_round
    if not isinstance(parsed, dict):
        return source, pr_loop_round
    if parsed.get("source") is not None:
        source = str(parsed["source"])
    round_value = parsed.get("pr_loop_round")
    if round_value is not None:
        pr_loop_round = int(round_value)
    return source, pr_loop_round


def review_verdict_from_phases(phase_results: list[dict[str, object]]) -> str | None:
    for item in reversed(phase_results):
        if item.get("phase") != "review":
            continue
        verdict = item.get("verdict")
        if verdict:
            return str(verdict)
    return None


def review_verdict_from_runner_result(result: FleetRunResult) -> str | None:
    for review in reversed(result.reviews):
        verdict = review.get("verdict")
        if verdict:
            return str(verdict)
    return None


def _should_record(repo: RepoConfig | None) -> bool:
    if repo is None:
        return True
    level_up_cfg = repo.level_up
    return level_up_cfg is None or level_up_cfg.train


def record_task_experience(
    *,
    task: FleetTask,
    status: str,
    phase_results: list[dict[str, object]] | None = None,
    changed_files: list[str] | tuple[str, ...] | None = None,
    workspace: Path | None = None,
    run_id: str | None = None,
    task_index: int | None = None,
) -> None:
    """Append experience for a FleetDispatcher task run."""
    repo = find_repo_config(workspace) if workspace is not None else None
    if not _should_record(repo):
        return

    repo_key_value = level_up_repo_key(
        name=repo.name if repo else None,
        repo_root=repo.repo_root if repo else None,
    )
    source, pr_loop_round = parse_experience_source_context(task.context)
    weight = compute_experience_weight(source, pr_loop_round, status=status)
    equip_snapshot = equip_snapshot_from_dispatch(task.equip)
    review_verdict = review_verdict_from_phases(phase_results or [])
    level_up_cfg = repo.level_up if repo is not None else None
    journal_summaries = level_up_cfg.journal_task_summaries if level_up_cfg is not None else True

    run_log = get_run_log()
    usage_rollup = (
        run_log.usage_rollup_snapshot(task_id=task_index) if run_log is not None else None
    )
    outcome_metrics = build_run_metrics(
        status=status,
        phases=phase_results,
        review_verdict=review_verdict,
        usage_rollup=usage_rollup,
        changed_files_count=len(changed_files or ()),
        repo_key=repo_key_value,
    )

    run_complete_data: dict[str, Any] = {
        "status": status,
        "equip_snapshot": equip_snapshot,
        "outcome_metrics": outcome_metrics,
    }
    if task_index is not None:
        run_complete_data["task_index"] = task_index
    if journal_summaries:
        run_complete_data["goal"] = task.goal

    append_journal(
        "run.complete",
        repo_key_value,
        task.persona,
        run_id=run_id,
        data=run_complete_data,
    )
    append_experience(
        repo_key=repo_key_value,
        persona=task.persona,
        source=source,
        weight=weight,
        pr_loop_round=pr_loop_round,
        status=status,
        goal=task.goal if journal_summaries else None,
        review_verdict=review_verdict,
        equip_snapshot=equip_snapshot,
        changed_files=changed_files,
        run_id=run_id,
        outcome_metrics=outcome_metrics,
    )
    append_journal(
        "experience.appended",
        repo_key_value,
        task.persona,
        run_id=run_id,
        data={"source": source, "weight": weight},
    )
    maybe_trigger_auto_learn(persona=task.persona, repo=repo)


def record_runner_experience(
    *,
    result: FleetRunResult,
    title: str,
    persona: str,
    repo_root: Path,
    experience_source: str = "full_pipeline",
    pr_loop_round: int | None = None,
    dispatch_equip: DispatchEquip | None = None,
) -> None:
    """Append experience for a LocalFleetRunner full-pipeline run."""
    repo = find_repo_config(repo_root)
    if not _should_record(repo):
        return

    repo_key_value = level_up_repo_key(
        name=repo.name if repo else None,
        repo_root=repo.repo_root if repo else repo_root,
    )
    weight = compute_experience_weight(experience_source, pr_loop_round, status=result.outcome)
    equip_snapshot = equip_snapshot_from_dispatch(dispatch_equip)
    review_verdict = review_verdict_from_runner_result(result)
    level_up_cfg = repo.level_up if repo is not None else None
    journal_summaries = level_up_cfg.journal_task_summaries if level_up_cfg is not None else True
    goal = title if journal_summaries else None

    run_log = get_run_log()
    usage_rollup = (
        run_log.usage_rollup_snapshot(task_id=result.task_id) if run_log is not None else None
    )
    outcome_metrics = build_run_metrics(
        status=result.outcome,
        phases=result.phases,
        error=result.error,
        pr_number=result.pr_number,
        review_verdict=review_verdict,
        usage_rollup=usage_rollup,
        changed_files_count=len(result.changed_files),
        duration_seconds=result.duration_seconds,
        repo_key=repo_key_value,
        issue_number=result.task_id,
    )

    append_journal(
        "run.complete",
        repo_key_value,
        persona,
        run_id=result.run_id,
        data={
            "status": result.outcome,
            "equip_snapshot": equip_snapshot,
            "goal": goal,
            "pipeline": "full",
            "outcome_metrics": outcome_metrics,
        },
    )
    append_experience(
        repo_key=repo_key_value,
        persona=persona,
        source=experience_source,
        weight=weight,
        pr_loop_round=pr_loop_round,
        status=result.outcome,
        goal=goal,
        review_verdict=review_verdict,
        equip_snapshot=equip_snapshot,
        changed_files=result.changed_files,
        run_id=result.run_id,
        outcome_metrics=outcome_metrics,
    )
    append_journal(
        "experience.appended",
        repo_key_value,
        persona,
        run_id=result.run_id,
        data={"source": experience_source, "weight": weight},
    )
    maybe_trigger_auto_learn(persona=persona, repo=repo)


def maybe_trigger_auto_learn(
    *,
    persona: str,
    repo: RepoConfig | None,
    fleet_config: FleetConfig | None = None,  # noqa: ARG001
) -> None:
    """Rate-limited cross-repo train when repo enables level_up.auto_learn."""
    if repo is None or repo.level_up is None or not repo.level_up.auto_learn:
        return

    from agent_fleet.learning import trigger_fleet_learning_cycle
    from agent_fleet.level_up.experience import read_experience_rows
    from agent_fleet.level_up.paths import LEVEL_UP_ROOT

    cooldown = repo.level_up.learn_cooldown_seconds
    min_rows = repo.level_up.min_experience_rows
    marker = LEVEL_UP_ROOT / "learning" / "last_auto_learn.json"
    marker.parent.mkdir(parents=True, exist_ok=True)

    now = time.time()
    if marker.is_file():
        try:
            last = json.loads(marker.read_text(encoding="utf-8"))
            if now - float(last.get("ts", 0)) < cooldown:
                return
        except json.JSONDecodeError, TypeError, ValueError:
            pass

    total_rows = 0
    repo_keys: set[str] = set()
    for repo_dir in LEVEL_UP_ROOT.iterdir():
        if not repo_dir.is_dir() or repo_dir.name == FLEET_TIER:
            continue
        exp_file = repo_dir / persona / "experience.jsonl"
        if not exp_file.is_file():
            continue
        rows = read_experience_rows(repo_dir.name, persona)
        if rows:
            total_rows += len(rows)
            repo_keys.add(repo_dir.name)

    if total_rows < min_rows or len(repo_keys) < repo.level_up.min_repos_for_fleet:
        return

    try:
        trigger_fleet_learning_cycle(personas=[persona])
        marker.write_text(
            json.dumps({"ts": now, "persona": persona, "total_rows": total_rows}),
            encoding="utf-8",
        )
        append_journal(
            "level_up.auto_learn.triggered",
            FLEET_TIER,
            persona,
            data={"total_rows": total_rows, "repo_keys": sorted(repo_keys)},
        )
    except Exception:
        pass
