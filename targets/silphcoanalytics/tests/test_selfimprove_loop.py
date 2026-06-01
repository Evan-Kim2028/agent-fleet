"""Unit tests for silphco.selfimprove.loop — end-to-end orchestrator.

LLMBackend and GitForge are injected mocks.  No real git, no real LLM,
no real promptfoo.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


from silphco.tests._mock_llm import MockGitForge, MockLLMBackend
from silphco.selfimprove.gate import EvalStats, GateResult
from silphco.selfimprove.loop import (
    _git_apply_diff,
    _pick_target_file,
    run_loop,
)
from silphco.selfimprove.mine import ErrorClass, FailureSignature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(
    *,
    persona: str = "backend",
    phase: str = "verify",
    status: str = "failed",
    event: str = "phase_end",
    detail: str | None = "schema_validation_failed",
    duration_s: float = 60.0,
    ts: str | None = None,
    run_id: str = "r001",
    issue: int = 1,
) -> dict:
    if ts is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record: dict = {
        "ts": ts,
        "run_id": run_id,
        "issue": issue,
        "persona": persona,
        "event": event,
        "phase": phase,
        "status": status,
        "duration_s": duration_s,
    }
    if detail is not None:
        record["detail"] = detail
    return record


_VALID_DIFF = """\
--- a/agents/personas/backend.md
+++ b/agents/personas/backend.md
@@ -1,3 +1,4 @@
 # Backend
+Always include severity.
 Content
"""

_GOOD_LLM_OUTPUT = f"""\
Root cause: missing severity hint.

```diff
{_VALID_DIFF}
```
"""

_GATE_PASS = GateResult(
    passed=True,
    reason="Gate passed.",
    frozen_before=EvalStats(total=2, passed=2),
    frozen_after=EvalStats(total=2, passed=2),
    target_after=EvalStats(total=2, passed=2),
)

_GATE_FAIL = GateResult(
    passed=False,
    reason="Target-signature pass-rate 0.0% < required minimum 50.0%.",
    frozen_before=EvalStats(total=2, passed=2),
    frozen_after=EvalStats(total=2, passed=2),
    target_after=EvalStats(total=2, passed=0),
)


def _write_log_with_records(
    path: Path,
    records: list[dict],
    count: int = 6,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_records = [dict(r, ts=ts, run_id=f"r{i}") for i, r in enumerate(records * count)]
    path.write_text("\n".join(json.dumps(r) for r in all_records) + "\n")


# ---------------------------------------------------------------------------
# _pick_target_file
# ---------------------------------------------------------------------------

class TestPickTargetFile:
    def test_picks_persona_file_for_known_persona(self):
        sig = FailureSignature("backend", "verify", ErrorClass.OTHER)
        assert _pick_target_file(sig) == "agents/personas/backend.md"

    def test_returns_none_when_persona_unknown_and_no_phase_prompts(self):
        sig = FailureSignature("unknown_persona", "plan", ErrorClass.OTHER)
        assert _pick_target_file(sig) is None

    def test_returns_none_when_both_unknown(self):
        sig = FailureSignature("unknown_persona", "unknown_phase", ErrorClass.OTHER)
        assert _pick_target_file(sig) is None


# ---------------------------------------------------------------------------
# run_loop — no-op when below threshold
# ---------------------------------------------------------------------------

class TestLoopNoOp:
    def test_no_pr_when_no_records(self, tmp_path: Path):
        log_path = tmp_path / "run_log.jsonl"
        log_path.write_text("")

        forge = MockGitForge()
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])
        result = run_loop(
            repo_root=tmp_path,
            backend=backend,
            forge=forge,
            log_path=log_path,
            min_occurrences=5,
        )

        assert result.prs_opened == []
        assert result.skipped_reason is not None
        assert "min_occurrences" in result.skipped_reason
        assert forge.opened_prs == []

    def test_no_pr_when_below_threshold(self, tmp_path: Path):
        log_path = tmp_path / "run_log.jsonl"
        # Only 3 records — below threshold of 5
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = [_make_record(ts=ts, run_id=f"r{i}") for i in range(3)]
        log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

        forge = MockGitForge()
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])
        result = run_loop(
            repo_root=tmp_path,
            backend=backend,
            forge=forge,
            log_path=log_path,
            min_occurrences=5,
        )

        assert result.prs_opened == []
        assert result.skipped_reason is not None
        assert forge.opened_prs == []

    def test_missing_log_file_is_no_op(self, tmp_path: Path):
        forge = MockGitForge()
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])
        result = run_loop(
            repo_root=tmp_path,
            backend=backend,
            forge=forge,
            log_path=tmp_path / "does_not_exist.jsonl",
        )
        assert result.prs_opened == []
        assert result.skipped_reason is not None


# ---------------------------------------------------------------------------
# run_loop — PR opened when gate passes (dry-run mode)
# ---------------------------------------------------------------------------

class TestLoopDryRun:
    def _write_enough_records(self, log_path: Path, count: int = 6) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = [_make_record(ts=ts, run_id=f"r{i}") for i in range(count)]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        # Create persona file
        persona_dir = log_path.parent / "agents" / "personas"
        persona_dir.mkdir(parents=True, exist_ok=True)
        (persona_dir / "backend.md").write_text("# Backend\nContent\n")

    def test_dry_run_returns_sentinel_pr_and_no_forge_calls(self, tmp_path: Path):
        log_path = tmp_path / "run_log.jsonl"
        self._write_enough_records(log_path)

        forge = MockGitForge()
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_PASS):
            result = run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
                dry_run=True,
            )

        assert -1 in result.prs_opened  # sentinel value for dry-run
        # forge must NOT have been called in dry-run mode
        assert forge.opened_prs == []

    def test_proposer_rejection_increments_rejected_count(self, tmp_path: Path):
        log_path = tmp_path / "run_log.jsonl"
        self._write_enough_records(log_path)

        forge = MockGitForge()
        # Return output with no diff → ProposerError
        backend = MockLLMBackend(responses=["No fix here — no diff block."])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_PASS):
            result = run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
                dry_run=True,
            )

        assert result.proposals_attempted >= 1
        assert result.proposals_rejected >= 1
        assert result.prs_opened == []

    def test_gate_rejection_increments_rejected_count(self, tmp_path: Path):
        log_path = tmp_path / "run_log.jsonl"
        self._write_enough_records(log_path)

        forge = MockGitForge()
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_FAIL):
            result = run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
                dry_run=True,
            )

        assert result.proposals_rejected >= 1
        assert result.prs_opened == []


# ---------------------------------------------------------------------------
# run_loop — PR opened (non-dry-run) with mocked git + forge
# ---------------------------------------------------------------------------

class TestLoopPROpened:
    def _setup(self, tmp_path: Path) -> Path:
        log_path = tmp_path / "run_log.jsonl"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = [_make_record(ts=ts, run_id=f"r{i}") for i in range(6)]
        log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        persona_dir = tmp_path / "agents" / "personas"
        persona_dir.mkdir(parents=True, exist_ok=True)
        (persona_dir / "backend.md").write_text("# Backend\nContent\n")
        return log_path

    def test_pr_opened_as_draft_when_gate_passes(self, tmp_path: Path):
        log_path = self._setup(tmp_path)
        forge = MockGitForge(pr_number=42)
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_PASS), \
             patch("silphco.selfimprove.loop._git_create_branch"), \
             patch("silphco.selfimprove.loop._git_apply_diff"), \
             patch("silphco.selfimprove.loop._git_commit"), \
             patch("subprocess.run"):
            result = run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
            )

        assert 42 in result.prs_opened
        assert len(forge.opened_prs) >= 1
        opened = forge.opened_prs[0]
        # NEVER auto-merge means always draft
        assert opened["draft"] is True

    def test_pr_body_contains_human_review_banner(self, tmp_path: Path):
        log_path = self._setup(tmp_path)
        forge = MockGitForge(pr_number=99)
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_PASS), \
             patch("silphco.selfimprove.loop._git_create_branch"), \
             patch("silphco.selfimprove.loop._git_apply_diff"), \
             patch("silphco.selfimprove.loop._git_commit"), \
             patch("subprocess.run"):
            run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
            )

        assert forge.opened_prs
        body = forge.opened_prs[0]["body"]
        assert "HUMAN REVIEW REQUIRED" in body or "human review" in body.lower()
        assert "AI-PROPOSED" in body or "ai-proposed" in body.lower()

    def test_pr_body_contains_signature_and_scores(self, tmp_path: Path):
        log_path = self._setup(tmp_path)
        forge = MockGitForge(pr_number=77)
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_PASS), \
             patch("silphco.selfimprove.loop._git_create_branch"), \
             patch("silphco.selfimprove.loop._git_apply_diff"), \
             patch("silphco.selfimprove.loop._git_commit"), \
             patch("subprocess.run"):
            run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
            )

        body = forge.opened_prs[0]["body"]
        assert "schema_validation_failed" in body
        assert "backend" in body
        assert "verify" in body

    def test_never_marks_ready_ie_never_automerges(self, tmp_path: Path):
        log_path = self._setup(tmp_path)
        forge = MockGitForge(pr_number=55)
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_PASS), \
             patch("silphco.selfimprove.loop._git_create_branch"), \
             patch("silphco.selfimprove.loop._git_apply_diff"), \
             patch("silphco.selfimprove.loop._git_commit"), \
             patch("subprocess.run"):
            run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
            )

        # mark_ready must NEVER be called (auto-merge would follow)
        assert forge.marked_ready == []

    def test_max_prs_cap_is_respected(self, tmp_path: Path):
        log_path = tmp_path / "run_log.jsonl"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Create 6 records for 2 different personas (each will be actionable)
        records = (
            [_make_record(ts=ts, persona="backend", phase="verify", run_id=f"r{i}") for i in range(6)]
            + [_make_record(ts=ts, persona="frontend", phase="implement", run_id=f"f{i}") for i in range(6)]
        )
        log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        persona_dir = tmp_path / "agents" / "personas"
        persona_dir.mkdir(parents=True, exist_ok=True)
        (persona_dir / "backend.md").write_text("# Backend\n")
        (persona_dir / "frontend.md").write_text("# Frontend\n")

        forge = MockGitForge(pr_number=1)
        backend = MockLLMBackend(responses=[_GOOD_LLM_OUTPUT])

        with patch("silphco.selfimprove.loop.run_gate", return_value=_GATE_PASS), \
             patch("silphco.selfimprove.loop._git_create_branch"), \
             patch("silphco.selfimprove.loop._git_apply_diff"), \
             patch("silphco.selfimprove.loop._git_commit"), \
             patch("subprocess.run"):
            result = run_loop(
                repo_root=tmp_path,
                backend=backend,
                forge=forge,
                log_path=log_path,
                max_prs=1,  # Hard cap
            )

        assert len(result.prs_opened) <= 1
        assert len(forge.opened_prs) <= 1


# ---------------------------------------------------------------------------
# _git_apply_diff — Finding 1b security tests
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo at *path* with an initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    # Initial commit so HEAD exists (needed for git diff HEAD)
    initial = path / "README.md"
    initial.write_text("initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


class TestGitApplyDiffSecurity:
    """Finding 1b: _git_apply_diff must not apply changes outside target_file."""

    def test_applies_valid_diff_to_target_file(self, tmp_path: Path) -> None:
        """A well-formed diff touching only the target file is applied cleanly."""
        _init_git_repo(tmp_path)
        target = tmp_path / "agents" / "personas" / "backend.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Backend\nold line\n")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add target"], cwd=str(tmp_path), check=True, capture_output=True)

        diff = (
            "--- a/agents/personas/backend.md\n"
            "+++ b/agents/personas/backend.md\n"
            "@@ -1,2 +1,3 @@\n"
            " # Backend\n"
            " old line\n"
            "+new line\n"
        )
        _git_apply_diff(diff, cwd=tmp_path, target_file="agents/personas/backend.md")
        assert "new line" in target.read_text()

    def test_manipulated_header_does_not_write_outside_target(self, tmp_path: Path) -> None:
        """A diff with a header pointing outside target_file must raise and revert.

        The ``--include=<target_file> --exclude='*'`` restriction should prevent
        the patch from applying to a different file.  If it did somehow apply,
        the post-apply ``git diff --name-only HEAD`` check must catch it and raise.
        """
        _init_git_repo(tmp_path)

        # Set up two committed files.
        target = tmp_path / "agents" / "personas" / "backend.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Backend\ncontent\n")
        decoy = tmp_path / "agents" / "personas" / "frontend.md"
        decoy.write_text("# Frontend\ncontent\n")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add files"], cwd=str(tmp_path), check=True, capture_output=True)

        # Diff that patches frontend.md but claims it is backend.md
        # (manipulated header — would exploit the old _git_apply_diff).
        manipulated_diff = (
            "--- a/agents/personas/frontend.md\n"
            "+++ b/agents/personas/frontend.md\n"
            "@@ -1,2 +1,3 @@\n"
            " # Frontend\n"
            " content\n"
            "+injected\n"
        )
        try:
            _git_apply_diff(
                manipulated_diff,
                cwd=tmp_path,
                target_file="agents/personas/backend.md",
            )
        except (RuntimeError, subprocess.CalledProcessError):
            # Either the --include filter caused git apply to fail (CalledProcessError)
            # or the post-apply check raised RuntimeError — both are correct.
            pass
        else:
            # If no exception was raised the patch must not have touched decoy.
            assert "injected" not in decoy.read_text(), (
                "_git_apply_diff silently applied a manipulated diff to a file "
                "outside target_file without raising"
            )

        # In all cases the working tree must be clean (no stray modifications).
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=True,
        )
        modified = [p for p in result.stdout.splitlines() if p]
        assert "agents/personas/frontend.md" not in modified, (
            "Working tree has stray modification to frontend.md after _git_apply_diff"
        )
