"""Converge a PR's post-merge labels onto exactly what its plan says.

Two rules, and the second is the one that matters:

* apply the plan's labels — ``table:<model>``, ``verify:<model>``,
  ``rebuild:heavy|light|none``;
* remove *stale* ones from older plans — a PR re-planned after new commits must
  not keep claiming ``table:gold_venues`` it no longer touches.

Removal is restricted to the prefixes this package owns. A human's ``bug`` or the
gate's ``premerge-approved`` label is not ours to delete.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_fleet.post_merge.types import LabelDelta, label_diff

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Called with (pr_number, add_labels, remove_labels) to converge one PR. The
#: default talks to ``gh``; tests pass a recorder.
ApplyFn = Callable[[int, "Sequence[str]", "Sequence[str]"], None]


class _GhLabeler:
    """Apply labels through the ``gh`` CLI."""

    def __init__(self, *, cwd: str = "", binary: str = "gh") -> None:
        self.cwd = cwd
        self.binary = binary

    def __call__(self, pr: int, add: Sequence[str], remove: Sequence[str]) -> None:
        for label in add:
            # --force so a label that does not exist yet is created rather than
            # failing the whole convergence.
            self._gh("label", "create", label, "--force")
        if add:
            self._gh("pr", "edit", str(pr), "--add-label", ",".join(add))
        if remove:
            self._gh("pr", "edit", str(pr), "--remove-label", ",".join(remove))

    def _gh(self, *args: str) -> None:
        result = subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            cwd=self.cwd or None,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-300:]
            raise RuntimeError(f"gh {' '.join(args)} failed (rc={result.returncode}): {detail}")


def gh_labeler(cwd: str = "") -> ApplyFn:
    """The real ``gh``-backed label applier."""
    return _GhLabeler(cwd=cwd)


def diff_for(current: Sequence[str], desired: Sequence[str]) -> LabelDelta:
    """The add/remove sets taking *current* onto *desired*."""
    return label_diff(tuple(current), tuple(desired))


def apply_labels(
    pr: int,
    *,
    current: Sequence[str],
    desired: Sequence[str],
    apply: ApplyFn,
) -> LabelDelta:
    """Converge PR *pr* onto *desired*, doing nothing when already correct.

    A no-op PR makes no ``gh`` call at all, which is what keeps a ten-PR batch
    to a handful of API writes instead of twenty.
    """
    delta = diff_for(current, desired)
    if delta.is_empty:
        return delta
    apply(pr, delta.add, delta.remove)
    return delta
