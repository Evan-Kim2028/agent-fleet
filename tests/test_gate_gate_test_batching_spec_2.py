"""Test for the spec-2 claim, part 1: the branch ships raw agent run-log
transcript files that the spec did not ask for, and ``.gitignore`` does not
cover ``.agent-fleet/``.

The claim: the PR adds 2,633 lines of agent run-log (impl.jsonl + impl.out) to
the repo; ``.gitignore`` ignores ``.agent-fleet-state.json`` but not
``.agent-fleet/``, so the transcript is committed and lands in the merged tree.

The repro assertion: ``git check-ignore .agent-fleet/runs/gate-test-batching/impl.jsonl``
-> no match.

Those repro assertions were inverted when ``origin/main`` landed the ignore rules
(``.agent-fleet/runs/`` and ``.agent-fleet/pr/``) and dropped the transcripts that
were already committed. The invariant the claim asks for -- run transcripts are
never committed and never committable -- now holds, so that is what is asserted
here: the runs dir is ignored, and no ``.agent-fleet/runs/`` file is tracked. The
tests fail again if either side regresses.

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


def test_transcript_path_is_covered_by_gitignore() -> None:
    """`.agent-fleet/runs/` is ignored, so transcripts are not committable."""
    result = _git("check-ignore", "-v", TRANSCRIPT)
    assert result.returncode == 0, (
        f"gitignore does not cover {TRANSCRIPT}, so a run transcript can be "
        "committed into the source tree; the claim asks for exactly this rule"
    )


def test_state_file_is_ignored_and_so_is_the_runs_dir() -> None:
    """Both the state file and the artifact tree beside it are ignored."""
    state = _git("check-ignore", "-q", ".agent-fleet-state.json")
    assert state.returncode == 0, "precondition: .agent-fleet-state.json is ignored"
    runs = _git("check-ignore", "-q", TRANSCRIPT_OUT)
    assert runs.returncode == 0, (
        f"{TRANSCRIPT_OUT} is not ignored; without a rule covering .agent-fleet/runs/, "
        "run transcripts can be committed to the repo"
    )


def test_no_run_transcript_is_tracked_in_the_tree() -> None:
    """No run-log transcript is tracked anywhere under the runs dir."""
    listed = _git("ls-tree", "-r", "--name-only", "HEAD")
    shipped = [line for line in listed.stdout.splitlines() if line.startswith(".agent-fleet/runs/")]
    assert not shipped, (
        f"run-log transcript(s) are tracked in the merged tree: {shipped}. They are "
        "machine-local state that changes on every dispatch and must never be committed."
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
