"""Unit tests for SilphCo-specific verification checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_fleet.contracts.verify_result import VerifySeverity
from agents.verify_checks import (
    VerifySettings,
    check_branch_sync,
    check_no_verifier_self_modify,
    make_check_diff_respects_allowed_paths,
)


class TestCheckNoVerifierSelfModify:
    """Regression for the verifier-attack vector (PR #812).

    Agents must not modify verify.py, dispatch.py, or any file under
    agents/agents/ to silence failing checks.
    """

    @pytest.fixture(autouse=True)
    def _verify_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Match .agent-fleet.yaml critical_path_prefixes post-migration."""
        settings = VerifySettings(
            protected_paths=(),
            override_label="agent-may-modify-fleet",
            revoke_label="agent-must-not-modify-fleet",
            verifier_escape_label="agent-can-modify-verifier",
            critical_path_prefixes=(
                "agents/agents/",
                "agents/silphco/",
                ".github/workflows/",
            ),
            secrets_patterns=(),
        )
        monkeypatch.setattr(
            "agents.verify_checks._load_verify_settings",
            lambda _path: settings,
        )

    def test_passes_when_no_critical_files_changed(self, tmp_path: Path) -> None:
        files = ["api/routes/auth.py", "frontend/src/App.tsx"]
        result = check_no_verifier_self_modify(tmp_path, files, issue_number=1)
        assert result.severity is VerifySeverity.OK
        assert result.name == "no_verifier_self_modify"

    def test_fatal_when_verify_py_modified(self, tmp_path: Path) -> None:
        files = ["agents/agents/verify.py"]
        with patch("agents.verify_checks._github_forge") as mock_forge:
            mock_forge.return_value.get_labels.return_value = []
            result = check_no_verifier_self_modify(tmp_path, files, issue_number=1)
        assert result.severity is VerifySeverity.FATAL
        assert "agents/agents/verify.py" in result.violating_paths
        assert "forbidden" in result.detail.lower()

    def test_fatal_when_any_file_under_agents_agents_changed(self, tmp_path: Path) -> None:
        files = ["agents/agents/logging.py", "agents/agents/phases.py"]
        with patch("agents.verify_checks._github_forge") as mock_forge:
            mock_forge.return_value.get_labels.return_value = []
            result = check_no_verifier_self_modify(tmp_path, files, issue_number=1)
        assert result.severity is VerifySeverity.FATAL
        assert len(result.violating_paths) == 2

    def test_fatal_when_agents_agents_file_modified(self, tmp_path: Path) -> None:
        files = ["agents/agents/dispatch.py"]
        with patch("agents.verify_checks._github_forge") as mock_forge:
            mock_forge.return_value.get_labels.return_value = []
            result = check_no_verifier_self_modify(tmp_path, files, issue_number=1)
        assert result.severity is VerifySeverity.FATAL
        assert "agents/agents/dispatch.py" in result.violating_paths

    def test_fatal_when_silphco_file_modified(self, tmp_path: Path) -> None:
        files = ["agents/silphco/verifier.py"]
        with patch("agents.verify_checks._github_forge") as mock_forge:
            mock_forge.return_value.get_labels.return_value = []
            result = check_no_verifier_self_modify(tmp_path, files, issue_number=1)
        assert result.severity is VerifySeverity.FATAL

    def test_escape_label_allows_modification(self, tmp_path: Path) -> None:
        files = ["agents/agents/verify.py"]
        with patch("agents.verify_checks._github_forge") as mock_forge:
            mock_forge.return_value.get_labels.return_value = ["agent-can-modify-verifier"]
            result = check_no_verifier_self_modify(tmp_path, files, issue_number=1)
        assert result.severity is VerifySeverity.OK
        assert "allowed by operator intent" in result.detail

    def test_does_not_use_agent_may_modify_fleet_label(self, tmp_path: Path) -> None:
        """The fleet override label must NOT bypass this check."""
        files = ["agents/agents/verify.py"]
        with patch("agents.verify_checks._github_forge") as mock_forge:
            mock_forge.return_value.get_labels.return_value = ["agent-may-modify-fleet"]
            result = check_no_verifier_self_modify(tmp_path, files, issue_number=1)
        assert result.severity is VerifySeverity.FATAL


class TestCheckBranchSync:
    """Smoke tests for the branch-sync check (git-dependent)."""

    def test_passes_when_no_commits_behind(self, tmp_path: Path) -> None:
        import subprocess

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        (tmp_path / "f").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        # No origin remote → merge-base fails → check returns OK (graceful)
        result = check_branch_sync(tmp_path, [], issue_number=1)
        assert result.severity is VerifySeverity.OK


class TestMakeCheckDiffRespectsAllowedPaths:
    def test_passes_when_all_files_in_allowed_paths(self, tmp_path: Path) -> None:
        check = make_check_diff_respects_allowed_paths(["api/", "pipeline/"])
        result = check(tmp_path, ["api/routes.py", "pipeline/src/x.py"], 1)
        assert result.severity is VerifySeverity.OK

    def test_retries_when_files_outside_allowed_paths(self, tmp_path: Path) -> None:
        check = make_check_diff_respects_allowed_paths(["api/"])
        result = check(tmp_path, ["api/routes.py", "frontend/src/App.tsx"], 1)
        assert result.severity is VerifySeverity.RETRY
        assert "frontend/src/App.tsx" in result.violating_paths

    def test_no_restriction_when_allowed_paths_empty(self, tmp_path: Path) -> None:
        check = make_check_diff_respects_allowed_paths([])
        result = check(tmp_path, ["any/file.py"], 1)
        assert result.severity is VerifySeverity.OK
        assert "No path restrictions" in result.detail
