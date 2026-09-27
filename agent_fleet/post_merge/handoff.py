"""Write the hand-off note a downstream data agent picks up.

This is the seam between "the code merged" and "the lake was rebuilt". A human
or an agent reading only the note must be able to answer: what landed, which
tables it touches, whether it needs a heavy or light rebuild, whether the deploy
passed, and which jobs are already queued for them.

One note per merged batch, plus exactly one line appended to ``INDEX`` — the
index is append-only so a downstream agent can tail it and never re-reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from agent_fleet.post_merge.planner import PlanResult
    from agent_fleet.post_merge.trigger import JobOutcome

#: The index file appended alongside the notes.
INDEX_NAME = "INDEX"


@dataclass(frozen=True)
class Batch:
    """One merged batch: the PRs, their plans, and what the deploy did."""

    repo: str
    main_sha: str
    deploy_rc: int
    results: Sequence[PlanResult]
    outcomes: Sequence[JobOutcome] = ()
    #: Set by the caller when this batch's notes are being re-emitted, so a
    #: second write is distinguishable from a second batch.
    note_id: str = ""

    @property
    def rebuild_tier(self) -> str:
        """The batch's rebuild tier: heavy if any PR was heavy, else light.

        A batch with nothing to rebuild is ``none``, which is a real answer the
        downstream agent needs — it means no lake work is pending, not that we
        forgot to look.
        """
        if any(r.plan.heavy for r in self.results):
            return "heavy"
        if any(r.plan.models or r.plan.jobs for r in self.results):
            return "light"
        return "none"


def _bullets(values: Sequence[str]) -> str:
    """A markdown bullet list, or an explicit "none" so absence is legible."""
    if not values:
        return "- (none)"
    return "\n".join(f"- {v}" for v in values)


def render_note(batch: Batch, *, generated_at: str = "") -> str:
    """Render one batch as the markdown note the downstream agent reads."""
    stamp = generated_at or datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_models: list[str] = []
    all_verify: list[str] = []
    for result in batch.results:
        all_models.extend(result.plan.models)
        all_verify.extend(result.plan.verify)
    models = sorted(dict.fromkeys(all_models))
    verify_only = sorted(dict.fromkeys(v for v in all_verify if v not in models))

    deploy = "ok" if batch.deploy_rc == 0 else f"FAILED (rc={batch.deploy_rc})"
    lines = [
        f"# post-merge: {batch.repo}",
        "",
        f"- batch: {batch.note_id or stamp}",
        f"- generated: {stamp}",
        f"- main: {batch.main_sha}",
        f"- deploy: {deploy}",
        f"- rebuild tier: {batch.rebuild_tier}",
        "",
        "## PRs",
        "",
    ]
    for result in batch.results:
        pr = result.pr_number
        lines.extend(
            [
                f"### #{pr} {result.pr.title}",
                "",
                f"- head sha: `{result.pr.head_sha}`",
                f"- merge commit: `{result.pr.merge_commit}`",
                f"- merged at: {result.pr.merged_at}",
                f"- rebuild: {result.plan.rebuild_tier}",
                "- tables:",
                _bullets(result.plan.models) if result.plan.models else "- (none)",
            ]
        )
        if result.plan.verify:
            lines.extend(["- verify-only tables:", _bullets(result.plan.verify)])
        lines.append("")

    lines.extend(
        [
            "## Tables",
            "",
            _bullets(models),
            "",
            "## Verify-only tables",
            "",
            _bullets(verify_only),
            "",
            "## Jobs queued",
            "",
        ]
    )
    if batch.outcomes:
        lines.extend(
            f"- {o.job} (slot {o.slot}): {o.status}{f' — {o.detail}' if o.detail else ''}"
            for o in batch.outcomes
        )
    else:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def index_line(batch: Batch) -> str:
    """The single INDEX line describing *batch*."""
    prs = ",".join(str(r.pr_number) for r in batch.results)
    queued = sum(1 for o in batch.outcomes if o.status == "ok")
    return (
        f"{batch.main_sha[:9]}  {batch.repo}  pr={prs}  "
        f"tier={batch.rebuild_tier}  deploy={batch.deploy_rc}  jobs={queued}"
    )


def write_note(batch: Batch, inbox: Path, *, generated_at: str = "") -> Path:
    """Write the note and append its INDEX line. Returns the note path.

    A second-resolution note id collides on a retry within the same second —
    which the ledger guarantees, since a retry sees its jobs as already
    triggered — so a free suffix is added rather than overwriting the record of
    which jobs actually ran. An identical re-run is the one exception: same
    content, same path, no second INDEX line, because there is no new fact.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    note_id = batch.note_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    body = render_note(batch, generated_at=generated_at or note_id)
    line = index_line(batch)
    name = f"{note_id}-{batch.repo}-pr{'-'.join(str(r.pr_number) for r in batch.results)}.md"
    path = inbox / name
    if _already_recorded(path, body, line):
        return path
    index = 1
    while path.exists():
        index += 1
        path = inbox / f"{name[:-3]}-{index}.md"
    path.write_text(body, encoding="utf-8")
    with (inbox / INDEX_NAME).open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return path


def _already_recorded(path: Path, body: str, line: str) -> bool:
    """True when this exact batch is already written at *path* and in INDEX."""
    if not path.exists():
        return False
    try:
        if path.read_text(encoding="utf-8") != body:
            return False
        return line in (path.parent / INDEX_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
