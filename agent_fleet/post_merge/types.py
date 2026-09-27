"""Value types for ``agent-fleet post-merge``.

Everything here is frozen with a ``to_dict`` so a plan or a hand-off note can be
serialised without the caller knowing the internal shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Label prefixes this package owns. Anything else on the PR is left alone, so
#: post-merge never strips a human's ``needs-review`` or the gate's status label.
TABLE_PREFIX = "table:"
VERIFY_PREFIX = "verify:"
REBUILD_PREFIX = "rebuild:"

_OWNED_PREFIXES = (TABLE_PREFIX, VERIFY_PREFIX, REBUILD_PREFIX)


@dataclass(frozen=True)
class Job:
    """One unit of rebuild work, identified by ``job`` and placed in a slot.

    ``job`` is the dedupe key: two PRs naming the same job produce one run.
    ``slot`` is the concurrency lane it belongs to, so jobs in different slots
    may run in parallel.
    """

    job: str
    slot: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {"job": self.job, "slot": self.slot}


@dataclass(frozen=True)
class Plan:
    """What one PR needs rebuilt, as returned by a repo's ``plan_command``."""

    models: tuple[str, ...] = ()
    jobs: tuple[Job, ...] = ()
    #: True when the change is expensive enough to need the heavy rebuild tier.
    heavy: bool = False
    #: Tables to check but not rebuild.
    verify: tuple[str, ...] = ()

    @property
    def rebuild_tier(self) -> str:
        """The ``rebuild:`` label value this plan implies."""
        if not self.models and not self.jobs:
            return "none"
        return "heavy" if self.heavy else "light"

    def labels(self) -> tuple[str, ...]:
        """The exact label set this plan owns, sorted and deduplicated."""
        labels = {f"{TABLE_PREFIX}{m}" for m in self.models if m}
        labels |= {f"{VERIFY_PREFIX}{v}" for v in self.verify if v}
        labels.add(f"{REBUILD_PREFIX}{self.rebuild_tier}")
        return tuple(sorted(labels))

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": list(self.models),
            "jobs": [j.to_dict() for j in self.jobs],
            "heavy": self.heavy,
            "verify": list(self.verify),
            "rebuild": self.rebuild_tier,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Plan:
        """Build a Plan from a planner's JSON, ignoring keys it does not know.

        A planner is an external command, so an extra key must not be fatal —
        but a malformed *known* key is, because silently dropping a model list
        would label a PR ``rebuild:none`` and skip its rebuild entirely.
        """
        jobs: list[Job] = []
        for entry in raw.get("jobs") or []:
            if not isinstance(entry, dict):
                raise ValueError("plan.jobs[] entries must be objects with 'job'")
            name = str(entry.get("job") or "").strip()
            if not name:
                raise ValueError("plan.jobs[] entry is missing a non-empty 'job'")
            jobs.append(Job(job=name, slot=str(entry.get("slot") or "default")))
        return cls(
            models=_str_tuple(raw.get("models"), "plan.models"),
            jobs=tuple(jobs),
            heavy=bool(raw.get("heavy", False)),
            verify=_str_tuple(raw.get("verify"), "plan.verify"),
        )


def _str_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(str(v) for v in value if str(v).strip())


@dataclass(frozen=True)
class MergedPR:
    """One PR in a merged batch, as read from the forge."""

    number: int
    title: str = ""
    head_sha: str = ""
    merge_commit: str = ""
    merged_at: str = ""
    files: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.number,
            "title": self.title,
            "head_sha": self.head_sha,
            "merge_commit": self.merge_commit,
            "merged_at": self.merged_at,
            "files": list(self.files),
            "labels": list(self.labels),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, number: int = 0) -> MergedPR:
        return cls(
            number=int(raw.get("number") or number),
            title=str(raw.get("title") or ""),
            # headRefOid is the spelling gh uses (PR_FIELDS in flow.py asks for
            # it); headSha is the older API's. Missing either one is a real
            # absence and stays "", which the planner refuses to plan on.
            head_sha=str(raw.get("headRefOid") or raw.get("headSha") or raw.get("head_sha") or ""),
            merge_commit=str(raw.get("mergeCommit") or raw.get("merge_commit") or ""),
            merged_at=str(raw.get("mergedAt") or raw.get("merged_at") or ""),
            files=_str_tuple(raw.get("files"), "pr.files"),
            labels=_str_tuple(raw.get("labels"), "pr.labels"),
        )


def parse_plan(stdout: str) -> Plan:
    """Parse a planner's stdout into a Plan.

    A planner that prints anything other than a JSON object has failed in a way
    the operator needs to see, so this raises rather than degrading to an empty
    plan — an empty plan labels a PR ``rebuild:none`` and silently skips it.
    """
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan_command did not print valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("plan_command must print a JSON object")
    return Plan.from_dict(raw)


def label_diff(current: tuple[str, ...] | list[str], desired: tuple[str, ...]) -> LabelDelta:
    """The add/remove sets that make *current* exactly *desired*.

    Only labels this package owns are ever considered for removal: a stale
    ``table:gold_sales`` from an older plan must go, a human's ``bug`` must not.
    """
    wanted = set(desired)
    owned_current = {label for label in current if label.startswith(_OWNED_PREFIXES)}
    return LabelDelta(
        add=tuple(sorted(wanted - set(current))),
        remove=tuple(sorted(owned_current - wanted)),
    )


@dataclass(frozen=True)
class LabelDelta:
    """The two label operations that converge a PR onto *desired*."""

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.add and not self.remove
