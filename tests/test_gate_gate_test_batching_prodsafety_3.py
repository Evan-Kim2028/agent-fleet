"""Test for the prodsafety-3 claim: the branch commits a raw agent-run transcript
under ``.agent-fleet/runs/``, a path that is not gitignored.

The claim: ``.agent-fleet/runs/gate-test-batching/impl.jsonl`` (+~2,600 lines,
1.9 MB) and ``impl.out`` are committed to the branch, ``.gitignore`` covers
``.agent-fleet-state.json`` but not ``.agent-fleet/runs/``, and an earlier run
artifact directory is already tracked on main -- so this is a pattern, not a
one-off.

These assertions fail on the current head (the path is neither ignored nor
absent from the branch) and pass once the artifacts are dropped and
``.agent-fleet/runs/`` is gitignored.

Note: the claim's secondary assertion that the gate caps diffs at 150000 chars
is NOT verifiable in the tree -- no such constant exists in ``agent_fleet/gate``
-- so it is deliberately not asserted here; only the committed-artifact and
ignore-rule facts are tested.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ".agent-fleet/runs"
BRANCH_ARTIFACTS = (
    ".agent-fleet/runs/gate-test-batching/impl.jsonl",
    ".agent-fleet/runs/gate-test-batching/impl.out",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False
    )


def test_runs_artifact_dir_is_not_gitignored() -> None:
    """`.gitignore` must cover the run-artifact directory.

    `.agent-fleet-state.json` is ignored today, but the sibling `.agent-fleet/`
    tree is not -- so a run transcript lands in the tree unless each author
    remembers to `rm` it.
    """
    ignored = _git("check-ignore", "-q", f"{ARTIFACT_DIR}/gate-test-batching/impl.jsonl")
    assert ignored.returncode != 0, (
        f"{ARTIFACT_DIR}/ is not gitignored, so agent run transcripts are committed "
        "to the repo; add it to .gitignore alongside .agent-fleet-state.json"
    )


def test_runs_artifact_dir_is_not_tracked_on_main() -> None:
    """A run-artifact directory should not be a tracked path on main either."""
    listed = _git("ls-tree", "-r", "--name-only", "origin/main")
    if listed.returncode != 0:  # pragma: no cover
        pytest.skip("origin/main not available in this checkout")
    tracked = [line for line in listed.stdout.splitlines() if line.startswith(ARTIFACT_DIR)]
    assert not tracked, (
        f"origin/main already tracks run artifacts under {ARTIFACT_DIR}: {tracked}. "
        "Committing another one on this branch makes it a pattern rather than a one-off."
    )


def test_branch_does_not_ship_the_run_transcript() -> None:
    """The PR must not carry a 1.9 MB agent transcript into the merged tree."""
    listed = _git("ls-tree", "-r", "--name-only", "HEAD")
    if listed.returncode != 0:  # pragma: no cover
        pytest.skip("HEAD not available in this checkout")
    shipped = [line for line in listed.stdout.splitlines() if line in BRANCH_ARTIFACTS]
    assert not shipped, (
        f"the branch ships raw agent run transcripts in the diff: {shipped}. "
        "Drop them from the branch and gitignore the directory."
    )


def test_run_transcript_does_not_dominate_the_change() -> None:
    """The transcript must not be the bulk of the PR's added lines.

    A ~2,600-line transcript dwarfing a ~400-line source change means reviewers
    (human or agent) spend their budget reading machine logs.
    """
    numstat = _git("diff", "--numstat", "origin/main...HEAD")
    if numstat.returncode != 0:  # pragma: no cover
        pytest.skip("origin/main not available in this checkout")

    added_artifacts = 0
    added_source = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, path = parts[0], parts[2]
        if not added.isdigit():
            continue
        if path.startswith(f"{ARTIFACT_DIR}/"):
            added_artifacts += int(added)
        elif path.endswith((".py", ".md", ".yaml", ".yml")):
            added_source += int(added)

    assert added_artifacts == 0 or added_artifacts < added_source, (
        f"run transcripts add {added_artifacts} lines vs {added_source} lines of real "
        "source/docs; the artifact dominates the change and should not be committed"
    )
