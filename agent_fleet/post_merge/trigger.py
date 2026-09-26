"""Trigger a repo's rebuild jobs, once each, after a successful deploy.

Two separate dedupes guard the rebuild:

* **within a batch** — three PRs touching ``gold_sales`` queue one job
  (:func:`agent_fleet.post_merge.planner.dedupe_jobs`);
* **across batches** — a job already triggered is recorded in a ledger and never
  re-run, so a retried or overlapping batch cannot double-fire a rebuild.

Jobs only run when the deploy returned 0. A failed deploy means main is not in a
rebuildable state, and queueing rebuilds against it would just produce work that
has to be thrown away.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agent_fleet.post_merge.config import RepoSpec

#: Called with the rendered argv to run one job. Tests substitute this.
TriggerFn = Callable[["Sequence[str]"], "subprocess.CompletedProcess[str]"]

#: Renders ``{job}`` and ``{slot}`` in a trigger command. A command with no
#: placeholder runs once per job with no argument substitution at all.
_TEMPLATE_FIELDS = {"job": "", "slot": "default"}


def render_trigger(template: str, *, job: str, slot: str) -> list[str]:
    """Expand a trigger template and split it, never handing it to a shell."""
    fields = {**_TEMPLATE_FIELDS, "job": job, "slot": slot}
    return shlex.split(template.format(**fields))


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=DEFAULT_TRIGGER_TIMEOUT,
    )


#: Ceiling for the default runner. A repo with heavier work sets
#: ``trigger_timeout_seconds`` and uses :func:`make_trigger` below.
DEFAULT_TRIGGER_TIMEOUT = 3600


def make_trigger(spec: RepoSpec) -> TriggerFn:
    """A trigger runner bound to *spec*'s timeout and checkout."""

    def _run_spec(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            cwd=spec.path or None,
            check=False,
            timeout=spec.trigger_timeout_seconds,
        )

    return _run_spec


@dataclass(frozen=True)
class JobOutcome:
    """What happened to one queued job."""

    job: str
    slot: str = "default"
    status: str = "skipped"
    detail: str = ""
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "skipped"}

    def to_dict(self) -> dict[str, object]:
        return {
            "job": self.job,
            "slot": self.slot,
            "status": self.status,
            "detail": self.detail,
            "returncode": self.returncode,
        }


def read_ledger(path: Path) -> set[str]:
    """Job names already triggered, from a one-per-line ledger."""
    if not path.exists():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _append_ledger(path: Path, job: str) -> None:
    """Record *job* as triggered. Best-effort: a lost ledger costs a re-run,
    a failed merge costs the whole batch."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{job}\n")
    except OSError:
        return


def run_jobs(
    spec: RepoSpec,
    jobs: Sequence[tuple[str, str]],
    *,
    deploy_rc: int,
    trigger: TriggerFn | None = None,
    ledger: Path | None = None,
) -> list[JobOutcome]:
    """Run each job at most once, and only when the deploy succeeded.

    A job that already fired is reported ``skipped``, so the hand-off note still
    lists it without pretending it was queued again.
    """
    if deploy_rc != 0:
        return [
            JobOutcome(job=name, slot=slot, status="skipped", detail=f"deploy rc={deploy_rc}")
            for name, slot in jobs
        ]

    ledger_path = ledger if ledger is not None else spec.ledger_path()
    already = read_ledger(ledger_path)
    run = trigger if trigger is not None else make_trigger(spec)

    outcomes: list[JobOutcome] = []
    for name, slot in jobs:
        if name in already:
            outcomes.append(JobOutcome(name, slot, "skipped", "already triggered"))
            continue
        if not spec.trigger_command:
            outcomes.append(JobOutcome(name, slot, "skipped", "no trigger_command configured"))
            continue
        result = run(render_trigger(spec.trigger_command, job=name, slot=slot))
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-300:]
            outcomes.append(JobOutcome(name, slot, "failed", detail, result.returncode))
            continue
        already.add(name)
        _append_ledger(ledger_path, name)
        outcomes.append(JobOutcome(name, slot, "ok", ""))
    return outcomes
