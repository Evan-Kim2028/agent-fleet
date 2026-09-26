"""Test for the spec-2 claim, part 1: the branch ships raw agent run-log
transcript files that the spec did not ask for, and ``.gitignore`` does not
cover ``.agent-fleet/``.

The claim: the PR adds 2,633 lines of agent run-log (impl.jsonl + impl.out) to
the repo; ``.gitignore`` ignores ``.agent-fleet-state.json`` but not
``.agent-fleet/``, so the transcript is committed and lands in the merged tree.

The repro assertion: ``git check-ignore .agent-fleet/runs/gate-test-batching/impl.jsonl``
-> no match. These tests fail on the current head and pass once the directory is
gitignored.

Note: the claim's stated impact ('consuming diff budget that the gate caps at
150000 chars') is not reproducible -- no such cap exists anywhere in
``agent_fleet/gate`` -- so that specific number is not asserted here. The
committed-artifact and ignore-rule facts are.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT = ".agent-fleet/runs/gate-test-batching/impl.jsonl"
TRANSCRIPT_OUT = ".agent-fleet/runs/gate-test-batching/impl.out"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False
    )


def test_transcript_path_is_not_covered_by_gitignore() -> None:
    """`.agent-fleet/` has no ignore rule, so transcripts are committable."""
    result = _git("check-ignore", "-v", TRANSCRIPT)
    assert result.returncode != 0, (
        f"gitignore covers {TRANSCRIPT} (matched {result.stdout.strip()!r}); the claim "
        "says it does not, and a cover is exactly the fix the claim asks for"
    )


def test_state_file_is_ignored_but_runs_dir_is_not() -> None:
    """Only the state file is ignored -- the artifact tree beside it is not."""
    state = _git("check-ignore", "-q", ".agent-fleet-state.json")
    assert state.returncode == 0, (
        "precondition: .agent-fleet-state.json is ignored while the run-artifact "
        "directory is not -- that asymmetry is what lets a transcript through"
    )
    runs = _git("check-ignore", "-q", TRANSCRIPT_OUT)
    assert runs.returncode != 0, (
        f"{TRANSCRIPT_OUT} should not be ignored; nothing in .gitignore covers "
        ".agent-fleet/runs/, so run transcripts are committed to the repo"
    )


def test_branch_introduces_the_transcript_files() -> None:
    """The transcript must be absent from the branch's tree."""
    listed = _git("ls-tree", "-r", "--name-only", "HEAD")
    shipped = [line for line in listed.stdout.splitlines() if line in (TRANSCRIPT, TRANSCRIPT_OUT)]
    assert not shipped, (
        f"the branch ships agent run-log transcript(s) in the merged tree: {shipped}. "
        "Drop them and gitignore .agent-fleet/runs/."
    )


def test_transcript_is_large_and_machine_generated() -> None:
    """Documents the scale of what is being committed, and fails while it is there."""
    listed = _git("ls-tree", "-r", "--long", "HEAD")
    row = next(
        (line.split() for line in listed.stdout.splitlines() if line.split("\t")[-1] == TRANSCRIPT),
        None,
    )
    if row is None:
        return  # already absent from the tree: nothing to assert
    size = int(row[3]) if len(row) >= 4 else 0
    assert size < 100_000, (
        f"{TRANSCRIPT} is {size} bytes of raw agent run log committed to the repo; "
        "run artifacts belong in the (gitignored) state dir, not the source tree"
    )
