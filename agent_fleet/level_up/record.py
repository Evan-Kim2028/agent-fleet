"""Record persona level-up journal and experience after a fleet run."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_fleet.level_up.experience import append_experience, compute_experience_weight
from agent_fleet.level_up.journal import append_journal
from agent_fleet.level_up.paths import repo_key as level_up_repo_key

if TYPE_CHECKING:
    from agent_fleet.repo import RepoConfig


def record_run_experience(
    *,
    repo: RepoConfig | None,
    persona: str,
    run_id: str,
    status: str,
    goal: str | None = None,
    source: str = "issue_dispatch",
    pr_loop_round: int | None = None,
    equip_snapshot: dict[str, Any] | None = None,
    review_verdict: str | None = None,
    changed_files: list[str] | None = None,
    task_index: int | None = None,
) -> None:
    """Append journal + experience rows when level-up training is enabled."""
    level_up_cfg = repo.level_up if repo is not None else None
    if level_up_cfg is not None and not level_up_cfg.train:
        return

    repo_key_value = level_up_repo_key(
        name=repo.name if repo else None,
        repo_root=repo.repo_root if repo else None,
    )
    weight = compute_experience_weight(source, pr_loop_round, status=status)
    journal_summaries = level_up_cfg.journal_task_summaries if level_up_cfg is not None else True

    run_complete_data: dict[str, Any] = {
        "status": status,
        "source": source,
    }
    if task_index is not None:
        run_complete_data["task_index"] = task_index
    if equip_snapshot:
        run_complete_data["equip_snapshot"] = equip_snapshot
    if journal_summaries and goal:
        run_complete_data["goal"] = goal

    append_journal(
        "run.complete",
        repo_key_value,
        persona,
        run_id=run_id,
        data=run_complete_data,
    )
    append_experience(
        repo_key=repo_key_value,
        persona=persona,
        source=source,
        weight=weight,
        pr_loop_round=pr_loop_round,
        status=status,
        goal=goal if journal_summaries else None,
        review_verdict=review_verdict,
        equip_snapshot=equip_snapshot,
        changed_files=changed_files,
        run_id=run_id,
    )
    append_journal(
        "experience.appended",
        repo_key_value,
        persona,
        run_id=run_id,
        data={"source": source, "weight": weight},
    )
