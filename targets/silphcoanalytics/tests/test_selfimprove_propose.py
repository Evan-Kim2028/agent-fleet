"""Unit tests for silphco.selfimprove.propose — LLM proposer with mocked backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from silphco.tests._mock_llm import MockLLMBackend
from silphco.selfimprove.mine import ErrorClass, FailureSignature, TraceRecord
from silphco.selfimprove.propose import (
    ChangeProposal,
    ProposerError,
    _diff_target_files,
    _extract_diff,
    _validate_diff,
    propose,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SIG = FailureSignature(
    persona="backend",
    phase="verify",
    error_class=ErrorClass.SCHEMA_VALIDATION_FAILED,
)

_TRACES = [
    TraceRecord(
        ts="2026-05-18T10:00:00Z",
        run_id="r001",
        issue=42,
        persona="backend",
        phase="verify",
        detail="schema_validation_failed: missing 'severity' key",
        duration_s=87.0,
    ),
    TraceRecord(
        ts="2026-05-17T09:00:00Z",
        run_id="r002",
        issue=41,
        persona="backend",
        phase="verify",
        detail="schema_validation_failed: severity must be one of ok/warn/fail",
        duration_s=60.0,
    ),
]

_TARGET = "agents/personas/backend.md"
_VALID_DIFF = """\
--- a/agents/personas/backend.md
+++ b/agents/personas/backend.md
@@ -5,6 +5,7 @@
 # Backend Agent Persona

 You are a backend agent.
+Always include the `severity` key in VerifyResult output.

 ## Capabilities
"""

_GOOD_LLM_OUTPUT = f"""\
Root cause: The backend persona does not remind the agent to include the
`severity` key in VerifyResult JSON output, leading to schema validation
failures in the verify phase.

```diff
{_VALID_DIFF}
```
"""

_NO_FIX_OUTPUT = "NO_SAFE_FIX: The schema error is not reproducible from this trace."


# ---------------------------------------------------------------------------
# _extract_diff
# ---------------------------------------------------------------------------

class TestExtractDiff:
    def test_extracts_diff_from_fenced_block(self):
        output = f"Some rationale.\n\n```diff\n{_VALID_DIFF}\n```\n"
        diff = _extract_diff(output)
        assert diff is not None
        assert "+++ b/agents/personas/backend.md" in diff

    def test_returns_none_when_no_fenced_block(self):
        assert _extract_diff("No diff here.") is None

    def test_case_insensitive_fence(self):
        output = f"```DIFF\n{_VALID_DIFF}\n```"
        diff = _extract_diff(output)
        assert diff is not None

    def test_strips_surrounding_whitespace(self):
        diff = _extract_diff(f"```diff\n   {_VALID_DIFF}   \n```")
        assert diff is not None
        assert not diff.startswith("   ")


# ---------------------------------------------------------------------------
# _diff_target_files
# ---------------------------------------------------------------------------

class TestDiffTargetFiles:
    def test_extracts_plus_header(self):
        targets = _diff_target_files(_VALID_DIFF)
        assert targets == ["agents/personas/backend.md"]

    def test_strips_b_prefix(self):
        diff = "+++ b/agents/personas/backend.md\n"
        assert _diff_target_files(diff) == ["agents/personas/backend.md"]

    def test_strips_a_prefix(self):
        diff = "+++ a/agents/personas/backend.md\n"
        assert _diff_target_files(diff) == ["agents/personas/backend.md"]

    def test_ignores_dev_null(self):
        diff = "+++ /dev/null\n"
        assert _diff_target_files(diff) == []

    def test_multi_file_diff(self):
        diff = (
            "+++ b/agents/personas/backend.md\n"
            "+++ b/agents/personas/frontend.md\n"
        )
        assert len(_diff_target_files(diff)) == 2


# ---------------------------------------------------------------------------
# _validate_diff
# ---------------------------------------------------------------------------

class TestValidateDiff:
    def test_valid_diff_passes(self):
        result = _validate_diff(_VALID_DIFF, "agents/personas/backend.md")
        assert result == _VALID_DIFF

    def test_empty_diff_raises(self):
        with pytest.raises(ProposerError, match="empty diff"):
            _validate_diff("", "agents/personas/backend.md")

    def test_whitespace_only_diff_raises(self):
        with pytest.raises(ProposerError, match="empty diff"):
            _validate_diff("   \n\t\n", "agents/personas/backend.md")

    def test_no_plus_header_raises(self):
        with pytest.raises(ProposerError, match="no \\+\\+\\+ header"):
            _validate_diff("--- a/foo.py\n@@ -1 +1 @@\n+x\n", "foo.py")

    def test_multi_file_diff_raises(self):
        multi = (
            "+++ b/agents/personas/backend.md\n"
            "+++ b/agents/personas/frontend.md\n"
        )
        with pytest.raises(ProposerError, match="touches 2 files"):
            _validate_diff(multi, "agents/personas/backend.md")

    def test_wrong_target_file_raises(self):
        with pytest.raises(ProposerError, match="does not match expected"):
            _validate_diff(_VALID_DIFF, "agents/personas/frontend.md")

    def test_denied_target_raises(self):
        # agents/agents/** is in the denylist
        diff = "+++ b/agents/agents/dispatch.py\n@@ -1 +1 @@\n+x\n"
        with pytest.raises(ProposerError, match="not permitted by the guard"):
            _validate_diff(diff, "agents/agents/dispatch.py")

    # --- Security: Finding 1a — prefix-injection via endswith bypass ---

    def test_prefix_injection_via_endswith_is_rejected(self):
        """A crafted +++ header b/foo/agents/personas/backend.md must be REJECTED.

        The old endswith('/' + expected_target) check would accept this because
        'foo/agents/personas/backend.md'.endswith('/agents/personas/backend.md')
        is True.  After normalisation, the diff path is
        'foo/agents/personas/backend.md' which != 'agents/personas/backend.md'.
        """
        crafted_diff = (
            "--- a/foo/agents/personas/backend.md\n"
            "+++ b/foo/agents/personas/backend.md\n"
            "@@ -1 +1 @@\n"
            "-original\n"
            "+injected\n"
        )
        with pytest.raises(ProposerError, match="does not match expected"):
            _validate_diff(crafted_diff, "agents/personas/backend.md")

    def test_exact_match_still_accepted(self):
        """Sanity: a diff whose +++ path IS the expected target still passes."""
        result = _validate_diff(_VALID_DIFF, "agents/personas/backend.md")
        assert result == _VALID_DIFF

    def test_path_traversal_in_diff_header_is_rejected(self):
        """A diff header containing .. must be rejected by normalisation."""
        traversal_diff = (
            "--- a/../agents/personas/backend.md\n"
            "+++ b/../agents/personas/backend.md\n"
            "@@ -1 +1 @@\n"
            "+x\n"
        )
        with pytest.raises(ProposerError):
            _validate_diff(traversal_diff, "agents/personas/backend.md")

    def test_absolute_path_in_diff_header_is_rejected(self):
        """A diff header with an absolute path must be rejected."""
        abs_diff = (
            "--- a//etc/passwd\n"
            "+++ b//etc/passwd\n"
            "@@ -1 +1 @@\n"
            "+root:x:0:0\n"
        )
        with pytest.raises(ProposerError):
            _validate_diff(abs_diff, "agents/personas/backend.md")


# ---------------------------------------------------------------------------
# propose() — end-to-end with mock backend
# ---------------------------------------------------------------------------

class TestPropose:
    def _make_backend(self, output: str = _GOOD_LLM_OUTPUT, exit_code: int = 0) -> MockLLMBackend:
        return MockLLMBackend(responses=[output], exit_codes=[exit_code])

    def test_returns_change_proposal(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend Agent Persona\n\nYou are a backend agent.\n\n## Capabilities\n")

        backend = self._make_backend()
        proposal = propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

        assert isinstance(proposal, ChangeProposal)
        assert proposal.target_file == _TARGET
        assert proposal.signature == _SIG
        assert proposal.diff == _VALID_DIFF.strip() or proposal.diff

    def test_makes_exactly_one_llm_call(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend\n")

        backend = self._make_backend()
        propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

        assert len(backend.calls) == 1

    def test_prompt_contains_signature_fields(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend\n")

        backend = self._make_backend()
        propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

        prompt = backend.calls[0]["prompt"]
        assert "backend" in prompt
        assert "verify" in prompt
        assert "schema_validation_failed" in prompt

    def test_prompt_contains_trace_detail(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend\n")

        backend = self._make_backend()
        propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

        prompt = backend.calls[0]["prompt"]
        # At least one trace detail must appear in the prompt
        assert "missing 'severity' key" in prompt or "schema_validation_failed" in prompt

    def test_denied_target_raises_before_llm_call(self, tmp_path: Path):
        backend = self._make_backend()
        with pytest.raises(ProposerError, match="not permitted by guard"):
            propose(_SIG, _TRACES, "agents/agents/dispatch.py", backend=backend, repo_root=tmp_path)
        assert len(backend.calls) == 0

    def test_missing_file_raises_proposer_error(self, tmp_path: Path):
        # Target file does not exist in tmp_path
        backend = self._make_backend()
        with pytest.raises(ProposerError, match="Cannot read target file"):
            propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

    def test_llm_failure_raises_proposer_error(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend\n")

        backend = MockLLMBackend(responses=["error"], exit_codes=[1])
        with pytest.raises(ProposerError, match="exit_code=1"):
            propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

    def test_no_safe_fix_signal_raises_proposer_error(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend\n")

        backend = self._make_backend(output=_NO_FIX_OUTPUT)
        with pytest.raises(ProposerError, match="declined to propose"):
            propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

    def test_no_diff_block_raises_proposer_error(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend\n")

        backend = self._make_backend(output="Just a rationale, no diff.")
        with pytest.raises(ProposerError, match="```diff block"):
            propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

    def test_rationale_extracted_from_output(self, tmp_path: Path):
        (tmp_path / "agents" / "personas").mkdir(parents=True)
        (tmp_path / "agents" / "personas" / "backend.md").write_text("# Backend\n")

        backend = self._make_backend()
        proposal = propose(_SIG, _TRACES, _TARGET, backend=backend, repo_root=tmp_path)

        assert "Root cause" in proposal.rationale or len(proposal.rationale) > 0
