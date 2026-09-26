"""Durable gate state: which stages finished, and at which head.

The gate pays for the same work twice whenever a run is repeated, and it
happens often:

* a **restart on the same head** re-dispatches every lens and every verifier,
  even though the stage that died may have finished the ones before it;
* a **rebase** produces a new head whose change is often *identical* — the
  thing that most often forces a rebase is the base moving under the PR — so
  a full find→verify→judge run rediscovers findings it already had.

Both are waste, and both are recoverable because a stage's result is a pure
function of the code it looked at. This module is the memory that makes a
repeat cheap: one small JSON marker per finished stage, named by the head it
was produced from.

Two keys, deliberately:

* :meth:`GateRunState.read` is keyed by **head sha**, and read only for the
  head now under review. That is same-head resume: the identical code, so the
  identical answer.
* :meth:`GateRunState.reusable_verified` is keyed by **patch-id**, and finds
  any head. That is rebase reuse: different history, same change.

Every read is a *miss on doubt*. A corrupt, truncated or partial marker reads
as "this stage did not finish" and the run redoes the work. A state module
that could fail a gate, or approve one, would be a worse bug than the waste it
removes — so every failure mode here is a silent miss.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - used at runtime (path.exists/stat/unlink)
from typing import Any

logger = logging.getLogger(__name__)

#: The stage that dispatched the lens reviewers. Its payload is the deduped,
#: truncated candidate list.
STAGE_FIND = "find"

#: The stage that turned candidates into evidence. Its payload is the whole
#: :class:`~agent_fleet.gate.pipeline._Evidence`: confirmed, untestable, and
#: the gate-written test files the confirmed blockers depend on.
STAGE_VERIFY = "verify"

_STATE_DIRNAME = "state"
_MARKER_PREFIX = "stage-"

#: Markers kept per stage. A stage needs the current head (same-head resume) and
#: one older head to look a matching patch-id up against; beyond that nothing
#: reads them.
_MAX_MARKERS = 3


@dataclass(frozen=True)
class StageState:
    """One finished stage: what it produced, and whether it may be reused."""

    head_sha: str
    stage: str
    payload: dict[str, Any] = field(default_factory=dict)
    outcome: str = ""
    infra_failed: bool = False
    patch_id: str = ""
    seq: int = 0

    @property
    def reusable(self) -> bool:
        """True when this stage finished cleanly enough to speak for a new head.

        Two things disqualify it, and they are different failures:

        * no recorded outcome — the run wrote the marker and then died before
          :meth:`~agent_fleet.gate.pipeline.GatePipeline._finish` could stamp
          the verdict, so what the marker holds is a stage that was mid-flight;
        * ``infra_failed`` — the run ended on a
          :class:`~agent_fleet.gate.pipeline.GateInfraError`, a dead or
          timed-out agent. Verification that never finished is not a result.

        A run that escalated *normally* (``untestable-needs-review``,
        ``stalled``) did finish verification, and its confirmed blockers are
        still real at the new head, so it stays reusable.
        """
        return bool(self.outcome) and not self.infra_failed


def _safe_name(stage: str) -> str:
    """Reduce a stage name to characters that are safe in a filename."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stage) or "stage"


class GateRunState:
    """Stage markers under ``<gate_dir>/state/``, one JSON file per stage.

    Follows the sibling-state convention already in the gate: the call
    transcripts live in ``<gate_dir>/calls/`` and the archived gate tests in
    ``<gate_dir>/tests/``, both written relative to the per-PR gate dir, so a
    later run can read what an earlier one left behind.
    """

    def __init__(self, gate_dir: Path) -> None:
        self.dir = gate_dir / _STATE_DIRNAME
        #: Monotonic write counter, stamped into every marker so "newest" is
        #: a fact rather than a filesystem timestamp guess. Seeded past the
        #: highest sequence already on disk, so a fresh process continues the
        #: ordering instead of restarting it.
        self._seq = self._highest_seq() + 1

    def _highest_seq(self) -> int:
        if not self.dir.is_dir():
            return 0
        highest = 0
        for path in self.dir.glob(f"{_MARKER_PREFIX}*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("seq"), int):
                highest = max(highest, data["seq"])
        return highest

    # -- paths ------------------------------------------------------------

    def path_for(self, stage: str, head_sha: str) -> Path:
        return self.dir / f"{_MARKER_PREFIX}{_safe_name(stage)}-{head_sha[:9]}.json"

    def _verify_markers(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(
            (p for p in self.dir.glob(f"{_MARKER_PREFIX}{_safe_name(STAGE_VERIFY)}-*.json")),
            key=self._recency,
            reverse=True,
        )

    @staticmethod
    def _recency(path: Path) -> tuple[int, float]:
        """Sort key for "which marker is newer".

        The recorded sequence is authoritative; mtime is only a fallback for a
        marker written by something else. Filesystem timestamps are not
        trustworthy for ordering here — a run writes its find and verify
        markers microseconds apart and the filesystem happily gives them the
        same value (verified: four files created in one tick share an identical
        ``st_mtime``), which would make "newest" arbitrary and let pruning
        delete the marker a reuse is about to need.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("seq"), int):
                return (data["seq"], path.stat().st_mtime)
        except OSError, ValueError:
            pass
        try:
            return (0, path.stat().st_mtime)
        except OSError:
            return (0, 0.0)

    # -- read -------------------------------------------------------------

    def read(self, stage: str, head_sha: str) -> StageState | None:
        """The marker for *stage* at exactly *head_sha*, or ``None``.

        Exact by construction: a marker written for one head is never returned
        for another, which is what keeps a same-head resume from becoming an
        unsound cross-head reuse.
        """
        return self._load(self.path_for(stage, head_sha))

    def _load(self, path: Path) -> StageState | None:
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            # Truncated, half-written, or not JSON at all. Indistinguishable
            # from "the stage never finished", which is exactly how it is
            # treated: the caller redoes the work.
            if path.exists():
                logger.warning(
                    "gate state marker unreadable, treating as absent: %s (%s)", path, exc
                )
            return None
        if not isinstance(data, dict):
            return None
        payload = data.get("payload")
        return StageState(
            head_sha=str(data.get("head_sha") or ""),
            stage=str(data.get("stage") or ""),
            payload=payload if isinstance(payload, dict) else {},
            outcome=str(data.get("outcome") or ""),
            infra_failed=bool(data.get("infra_failed")),
            patch_id=str(data.get("patch_id") or ""),
            seq=data.get("seq") if isinstance(data.get("seq"), int) else 0,
        )

    def reusable_verified(self, patch_id: str) -> StageState | None:
        """Verification evidence for *patch_id* from any head, if trustworthy.

        The rebase-reuse query. Scans verify markers newest-first and returns
        the first one that :attr:`StageState.reusable` accepts *and* whose
        recorded patch-id matches — a marker from a different change is a
        different PR's evidence, and reusing it would review one diff against
        another's findings.
        """
        if not patch_id:
            return None
        for path in self._verify_markers():
            state = self._load(path)
            if state is None or not state.reusable:
                continue
            if state.patch_id == patch_id:
                return state
        return None

    # -- write ------------------------------------------------------------

    def write(
        self,
        stage: str,
        head_sha: str,
        payload: dict[str, Any] | None = None,
        *,
        outcome: str = "",
        infra_failed: bool = False,
        patch_id: str = "",
        seq: int | None = None,
    ) -> None:
        """Record a finished stage, atomically.

        Written to a sibling temp file and renamed, so a gate killed mid-write
        leaves either the previous marker or none — never a truncated file that
        parses as "this stage finished with less evidence than it has".

        *seq* is assigned on first write and preserved on the re-write that
        stamps a run's verdict, so finishing a marker does not make it look
        newer than the head it was actually produced for.
        """
        record = {
            "stage": stage,
            "head_sha": head_sha,
            "seq": self._seq if seq is None else seq,
            "payload": payload or {},
            "outcome": outcome,
            "infra_failed": bool(infra_failed),
            "patch_id": patch_id,
        }
        self._seq += 1
        target = self.path_for(stage, head_sha)
        tmp = target.with_suffix(".json.tmp")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
            tmp.replace(target)
        except (OSError, TypeError) as exc:
            # A marker we cannot write only costs a later run its shortcut.
            logger.warning("gate could not persist %s stage state: %s", stage, exc)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def mark_verified(
        self,
        head_sha: str,
        payload: dict[str, Any],
        *,
        patch_id: str,
        outcome: str,
        infra_failed: bool = False,
    ) -> None:
        """Record completed verification at *head_sha* for later reuse."""
        self.write(
            STAGE_VERIFY,
            head_sha,
            payload,
            outcome=outcome,
            infra_failed=infra_failed,
            patch_id=patch_id,
        )

    def stamp_outcome(self, head_sha: str, *, outcome: str, infra_failed: bool) -> None:
        """Fill in the verdict and crash flag on the markers this run wrote.

        A stage cannot know the run's final outcome when it finishes, so
        :meth:`~agent_fleet.gate.pipeline.GatePipeline._finish` calls this
        afterwards. Markers written by a run that then died keep their empty
        outcome, which :attr:`StageState.reusable` refuses.
        """
        for stage in (STAGE_FIND, STAGE_VERIFY):
            current = self.read(stage, head_sha)
            if current is None:
                continue
            self.write(
                stage,
                head_sha,
                current.payload,
                outcome=outcome,
                infra_failed=infra_failed,
                patch_id=current.patch_id,
                seq=current.seq or None,
            )

    # -- lifecycle --------------------------------------------------------

    def prune(self, keep: int = _MAX_MARKERS) -> None:
        """Keep the newest *keep* markers per stage; drop the rest.

        Reuse needs the last run's markers to survive, so this cannot simply
        clear. But nothing reads a marker for a head twenty rebases back, and
        the directory is per-PR and never otherwise emptied. Bounding it by
        stage is enough: a single stage can only need the current head (same-head
        resume) plus one older head to find a patch-id match against.
        """
        if not self.dir.is_dir():
            return
        for stage in (STAGE_FIND, STAGE_VERIFY):
            paths = sorted(
                (p for p in self.dir.glob(f"{_MARKER_PREFIX}{_safe_name(stage)}-*.json")),
                key=self._recency,
                reverse=True,
            )
            for stale in paths[keep:]:
                try:
                    stale.unlink()
                except OSError as exc:
                    logger.warning("gate could not prune stage state %s: %s", stale, exc)

    def clear(self) -> None:
        """Drop every marker. For tests, and for an operator resetting a PR."""
        if not self.dir.is_dir():
            return
        for path in self.dir.glob(f"{_MARKER_PREFIX}*.json*"):
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("gate could not clear stage state %s: %s", path, exc)
