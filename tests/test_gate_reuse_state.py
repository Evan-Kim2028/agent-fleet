"""Reuse across a rebase and across a restart, and the check that replaces both.

Three costs the gate used to pay twice:

* a PR that conflicts with its base was only found *after* a full
  find→verify→judge run;
* a rebased PR whose own change is byte-identical re-bought the whole review;
* a gate re-launched on the same head restarted every stage.

Each is exercised here against a **real temporary git repository**, because
all three are claims about what git actually reports — a mocked ``subprocess``
would only test the mock, and the whole failure mode being prevented (exit code
1 meaning "conflict" while every other non-zero means "git broke") lives in
git's exit codes.

``tmp_path`` is redirected off ``/tmp`` for the same reason the rest of the
machine's git work happens under ``$HOME/fleet``: ``/tmp`` is RAM here, and a
git repository full of objects in tmpfs is both slow and a memory hazard. Run
these with ``--basetemp=$HOME/fleet/tmp/pytest-base/gate-reuse``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path  # noqa: TC003 - used at runtime (temp git repos)

import pytest

from agent_fleet.contracts.gate import GateOutcome
from agent_fleet.gate import gitops
from agent_fleet.gate.gitops import MergeConflict, merge_conflict_check
from agent_fleet.gate.state import STAGE_FIND, STAGE_VERIFY, GateRunState, StageState

_PYPROJECT = """[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "reuse-fixture"
version = "0.0.0"
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout.strip()


class _Repo:
    """A main branch, a PR branch, and the two ways a PR's head moves."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main", str(root))
        _git(root, "config", "user.email", "gate@test.local")
        _git(root, "config", "user.name", "Gate Test")
        (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
        (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        _git(root, "checkout", "-q", "-b", "fb/lane")
        self.commit("pr change", "agent.py", "VALUE = 2\n")

    def commit(self, message: str, name: str, content: str) -> str:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def checkout(self, branch: str) -> None:
        _git(self.root, "checkout", "-q", branch)

    def head_of(self, branch: str) -> str:
        return _git(self.root, "rev-parse", branch)

    def move_main(self, content: str, *, name: str = "other.py") -> None:
        """Advance main, then come back to the PR branch."""
        current = _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        _git(self.root, "checkout", "-q", "main")
        self.commit("main moves on", name, content)
        _git(self.root, "checkout", "-q", current)

    def merge_main(self) -> str:
        """Merge main into the PR branch — the rebase-by-merge case."""
        self.move_main("UNRELATED = 1\n")
        _git(self.root, "merge", "-q", "--no-edit", "main")
        return self.head()


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    return _Repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# 1. The early conflict check
# ---------------------------------------------------------------------------


def test_a_pr_that_merges_cleanly_reports_no_conflict(repo: _Repo) -> None:
    head = repo.head()
    result = merge_conflict_check(repo.root, head, "main")
    assert result == MergeConflict(conflict_files=(), git_error=False)


def test_a_merge_of_an_unchanged_base_is_clean(repo: _Repo) -> None:
    """A moved base that touches other files still merges cleanly.

    The common rebase case: main moved, but not where the PR did. Reporting
    this as a conflict would escalate every rebased PR in the fleet.
    """
    head = repo.head()
    repo.move_main("UNRELATED = 1\n")
    result = merge_conflict_check(repo.root, head, "main")
    assert result == MergeConflict(conflict_files=(), git_error=False)


def test_a_genuine_conflict_names_the_files(repo: _Repo) -> None:
    """Two branches rewriting the same line is a conflict, and it is exit 1."""
    pr_head = repo.head()
    repo.move_main("VALUE = 99\n", name="agent.py")  # main edits what the PR edited
    result = merge_conflict_check(repo.root, pr_head, "main")
    assert result.conflict_files, "a real conflict must be reported as one"
    assert "agent.py" in result.conflict_files
    assert result.git_error is False


def test_a_bad_ref_is_a_git_error_and_never_a_conflict(repo: _Repo) -> None:
    """The failure this whole dataclass exists to prevent.

    ``git merge-tree`` exits **1** for an unresolvable ref, exactly as it does
    for a conflict — the only difference is that stdout is empty. Reading the
    exit code alone would send every healthy PR to a rebase agent for ever.
    """
    result = merge_conflict_check(repo.root, repo.head(), "no-such-branch")
    assert result.git_error is True
    assert result.conflict_files == ()


def test_an_unknown_head_sha_is_a_git_error_not_a_conflict(repo: _Repo) -> None:
    result = merge_conflict_check(repo.root, "0" * 40, "main")
    assert result.git_error is True
    assert result.conflict_files == ()


def test_a_missing_tree_oid_means_git_error_even_with_output() -> None:
    """The parser's own guard, in isolation.

    Anything that is not a sha on the first line means git never produced a
    merged tree, whatever the exit code said.
    """
    from agent_fleet.gate.gitops import _looks_like_a_tree_oid

    assert _looks_like_a_tree_oid("a" * 40)
    assert _looks_like_a_tree_oid("b" * 64)
    assert not _looks_like_a_tree_oid("")
    assert not _looks_like_a_tree_oid("agent.py")
    assert not _looks_like_a_tree_oid("merge-tree: no-such-branch - not something we can merge")
    assert not _looks_like_a_tree_oid("z" * 40)  # not hex
    assert not _looks_like_a_tree_oid("a" * 39)  # wrong length


# ---------------------------------------------------------------------------
# 2. patch-id survives the merge that a rebase produces
# ---------------------------------------------------------------------------


def test_patch_id_is_stable_across_a_merge_commit(repo: _Repo) -> None:
    """The rebase-reuse identity, on a real merge rather than a rebase.

    This is the case reuse exists for: the PR's own change is untouched, only
    its history moved, and re-reviewing it would buy nothing.
    """
    old = repo.head()
    new = repo.merge_main()
    assert old != new, "the merge must actually have changed the head"
    assert gitops.patch_id(repo.root, old, "main") == gitops.patch_id(repo.root, new, "main")


def test_patch_id_differs_when_the_pr_change_differs(repo: _Repo) -> None:
    old = repo.head()
    repo.merge_main()
    new = repo.commit("a real new change", "agent.py", "VALUE = 3\n")
    assert gitops.patch_id(repo.root, old, "main") != gitops.patch_id(repo.root, new, "main")


# ---------------------------------------------------------------------------
# 3. Stage markers
# ---------------------------------------------------------------------------


def test_a_marker_round_trips_its_payload(tmp_path: Path) -> None:
    state = GateRunState(tmp_path)
    state.write(STAGE_FIND, "abc123def", {"candidates": [{"id": "f1"}]})
    read = state.read(STAGE_FIND, "abc123def")
    assert read is not None
    assert read.payload == {"candidates": [{"id": "f1"}]}
    assert read.head_sha == "abc123def"


def test_a_marker_is_never_read_for_a_different_head(tmp_path: Path) -> None:
    """Exact-sha keying: this is what keeps resume from becoming unsound reuse."""
    state = GateRunState(tmp_path)
    state.write(STAGE_FIND, "abc123def", {"candidates": []})
    assert state.read(STAGE_FIND, "999999999") is None


def test_a_corrupt_marker_reads_as_absent_rather_than_raising(tmp_path: Path) -> None:
    """Truncated state must cost a re-run, never a crash.

    State is an optimisation; an optimisation that can fail a gate is worse
    than the waste it removes.
    """
    state = GateRunState(tmp_path)
    state.write(STAGE_VERIFY, "abc123def", {"evidence": {}})
    state.path_for(STAGE_VERIFY, "abc123def").write_text("{ not json", encoding="utf-8")
    assert state.read(STAGE_VERIFY, "abc123def") is None


def test_a_marker_holding_a_non_object_reads_as_absent(tmp_path: Path) -> None:
    state = GateRunState(tmp_path)
    state.write(STAGE_VERIFY, "abc123def", {"evidence": {}})
    state.path_for(STAGE_VERIFY, "abc123def").write_text("[1, 2, 3]", encoding="utf-8")
    assert state.read(STAGE_VERIFY, "abc123def") is None


def test_an_unwritable_state_dir_does_not_raise(tmp_path: Path) -> None:
    """A marker we cannot write only costs a later run its shortcut."""
    state = GateRunState(tmp_path)
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")
    state.write(STAGE_FIND, "abc123def", {"candidates": []})
    assert state.read(STAGE_FIND, "abc123def") is None


def test_no_marker_is_written_leaving_a_temp_file(tmp_path: Path) -> None:
    state = GateRunState(tmp_path)
    state.write(STAGE_FIND, "abc123def", {"candidates": []})
    assert not list(state.dir.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 4. What counts as reusable evidence
# ---------------------------------------------------------------------------


def test_a_finished_verification_is_reusable_for_the_same_patch(tmp_path: Path) -> None:
    state = GateRunState(tmp_path)
    state.mark_verified(
        "old_head_sha",
        {"evidence": {"confirmed": [{"id": "f1"}]}},
        patch_id="patch-abc",
        outcome=GateOutcome.NEEDS_ESCALATION.value,
    )
    found = state.reusable_verified("patch-abc")
    assert found is not None
    assert found.head_sha == "old_head_sha"


def test_evidence_from_a_different_change_is_never_reused(tmp_path: Path) -> None:
    """Reusing another diff's findings would review one change by another's lights."""
    state = GateRunState(tmp_path)
    state.mark_verified("old", {"evidence": {}}, patch_id="patch-abc", outcome="NEEDS-ESCALATION")
    assert state.reusable_verified("patch-xyz") is None


def test_verification_from_a_crashed_run_is_not_reused(tmp_path: Path) -> None:
    """A dead or timed-out agent is not a verification result.

    ``infra_failed`` is the whole gate on reuse: a run that died mid-stage
    never finished verifying, so its marker is a crash artefact.
    """
    state = GateRunState(tmp_path)
    state.mark_verified(
        "old",
        {"evidence": {}},
        patch_id="patch-abc",
        outcome=GateOutcome.NEEDS_ESCALATION.value,
        infra_failed=True,
    )
    assert state.reusable_verified("patch-abc") is None


def test_an_unfinished_run_is_not_reused(tmp_path: Path) -> None:
    """No recorded outcome means the run died before stamping one."""
    state = GateRunState(tmp_path)
    state.mark_verified("old", {"evidence": {}}, patch_id="patch-abc", outcome="")
    assert state.reusable_verified("patch-abc") is None


def test_an_empty_patch_id_matches_nothing(tmp_path: Path) -> None:
    """``patch_id`` returns "" when it cannot be computed; "" must match nothing,
    or an unknown commit would look like every commit."""
    state = GateRunState(tmp_path)
    state.mark_verified("old", {"evidence": {}}, patch_id="", outcome=GateOutcome.APPROVED.value)
    assert state.reusable_verified("") is None


def test_the_newest_reusable_marker_wins(tmp_path: Path) -> None:
    state = GateRunState(tmp_path)
    state.mark_verified("older", {"evidence": {"n": 1}}, patch_id="p", outcome="NEEDS-ESCALATION")
    state.mark_verified("newer", {"evidence": {"n": 2}}, patch_id="p", outcome="NEEDS-ESCALATION")
    found = state.reusable_verified("p")
    assert found is not None
    assert found.head_sha == "newer"


def test_stamping_outcome_makes_a_run_reusable(tmp_path: Path) -> None:
    """The end-to-end path: a stage writes an unstamped marker, the run's
    verdict is stamped on the way out, and only then may a later head use it."""
    state = GateRunState(tmp_path)
    state.mark_verified("head1", {"evidence": {}}, patch_id="p", outcome="")
    assert state.reusable_verified("p") is None
    state.stamp_outcome("head1", outcome=GateOutcome.APPROVED.value, infra_failed=False)
    assert state.reusable_verified("p") is not None


def test_prune_bounds_the_directory_without_dropping_recent_markers(tmp_path: Path) -> None:
    """Reuse needs the last run's markers to survive, so pruning keeps some."""
    state = GateRunState(tmp_path)
    # Full-length shas: the marker filename is the 9-char prefix, so short
    # stand-ins like "head0"/"head1" would collide into one file.
    shas = [f"{i}" * 40 for i in range(1, 7)]
    for sha in shas:
        state.mark_verified(sha, {"evidence": {}}, patch_id="p", outcome="APPROVED")
        state.write(STAGE_FIND, sha, {"candidates": []})
    state.prune(keep=2)
    assert len(list(state.dir.glob(f"stage-{STAGE_VERIFY}-*.json"))) == 2
    assert state.read(STAGE_VERIFY, shas[5]) is not None
    assert state.read(STAGE_VERIFY, shas[0]) is None


def test_clear_removes_everything(tmp_path: Path) -> None:
    state = GateRunState(tmp_path)
    state.mark_verified("a", {"evidence": {}}, patch_id="p", outcome="APPROVED")
    state.write(STAGE_FIND, "a", {"candidates": []})
    state.clear()
    assert state.read(STAGE_VERIFY, "a") is None
    assert state.read(STAGE_FIND, "a") is None


# ---------------------------------------------------------------------------
# 5. The evidence payload the markers carry
# ---------------------------------------------------------------------------


def test_evidence_survives_the_round_trip_to_json() -> None:
    """The reuse payload is the evidence the gate acts on, so a lossy
    serialisation would reuse a verdict rather than a set of findings."""
    from agent_fleet.gate.pipeline import _Evidence

    original = _Evidence(
        confirmed=[{"id": "f1", "claim": "x", "test_file": "tests/test_gate_fb_lane_c_1.py"}],
        untestable=[{"id": "f2", "claim": "y"}],
        gate_tests=["tests/test_gate_fb_lane_c_1.py"],
    )
    restored = _Evidence.restore(json.loads(json.dumps(original.to_payload())))
    assert restored.confirmed == original.confirmed
    assert restored.untestable == original.untestable
    assert restored.gate_tests == original.gate_tests


def test_restoring_a_malformed_evidence_payload_drops_the_bad_parts() -> None:
    from agent_fleet.gate.pipeline import _Evidence

    restored = _Evidence.restore(
        {"confirmed": ["not a dict"], "untestable": None, "gate_tests": [7]}
    )
    assert restored.confirmed == []
    assert restored.untestable == []
    assert restored.gate_tests == ["7"]


def test_stage_state_reusable_requires_both_a_verdict_and_no_infra_failure() -> None:
    assert StageState(head_sha="a", stage="verify", outcome="APPROVED").reusable is True
    assert StageState(head_sha="a", stage="verify", outcome="").reusable is False
    assert (
        StageState(head_sha="a", stage="verify", outcome="APPROVED", infra_failed=True).reusable
        is False
    )


def test_a_normal_escalation_is_still_reusable(tmp_path: Path) -> None:
    """``untestable-needs-review`` and friends did finish verification, so their
    confirmed blockers are still real at the new head. Only a crash disqualifies."""
    state = GateRunState(tmp_path)
    for outcome in ("untestable-needs-review", "stalled", "no-push", "tests-broken"):
        state.mark_verified(
            f"h-{outcome}", {"evidence": {}}, patch_id=f"p-{outcome}", outcome=outcome
        )
        assert state.reusable_verified(f"p-{outcome}") is not None
