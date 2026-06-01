"""Tests for agents/agents/verify_checks.py — silphco-specific verify checks."""

from __future__ import annotations

from unittest.mock import patch

from agent_fleet.contracts.verify_result import VerifySeverity
from agents.verify_checks import (
    check_error_boundary_tests,
    check_integration_tests_for_chat_changes,
    check_no_agent_infrastructure_changes,
    check_tool_coverage_on_removal,
)


# ---------------------------------------------------------------------------
# _get_protected_paths from TOML config
# ---------------------------------------------------------------------------


class TestGetProtectedPaths:
    def test_contains_agents_agents_prefix(self):
        from agents.verify_checks import _get_protected_paths
        paths = _get_protected_paths()
        assert any(p.startswith("agents/agents/") for p in paths)

    def test_contains_agents_tests_prefix(self):
        from agents.verify_checks import _get_protected_paths
        paths = _get_protected_paths()
        assert any(p.startswith("agents/tests/") for p in paths)

    def test_contains_agents_personas_prefix(self):
        from agents.verify_checks import _get_protected_paths
        paths = _get_protected_paths()
        assert any(p.startswith("agents/personas/") for p in paths)

    def test_contains_github_workflows_prefix(self):
        from agents.verify_checks import _get_protected_paths
        paths = _get_protected_paths()
        assert any(p.startswith(".github/workflows/") for p in paths)

    def test_returns_tuple(self):
        from agents.verify_checks import _get_protected_paths
        paths = _get_protected_paths()
        assert isinstance(paths, tuple)


# ---------------------------------------------------------------------------
# check_no_agent_infrastructure_changes
# ---------------------------------------------------------------------------


class TestCheckNoAgentInfrastructureChanges:
    def test_passes_when_no_protected_paths_touched(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/agents/",)):
            result = check_no_agent_infrastructure_changes(
                tmp_path, ["frontend/src/App.tsx", "api/main.py"], issue_number=1
            )
        assert result.severity is VerifySeverity.OK
        assert result.passed is True
        assert result.name == "no_agent_infrastructure_changes"

    def test_empty_file_list_passes(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/agents/",)):
            result = check_no_agent_infrastructure_changes(tmp_path, [], issue_number=1)
        assert result.severity is VerifySeverity.OK
        assert result.passed is True

    def test_protected_path_is_fatal(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/agents/",)):
            with patch("agents.verify_checks.GHForge") as mock_gh:
                mock_gh.return_value.get_labels.return_value = []
                result = check_no_agent_infrastructure_changes(
                    tmp_path, ["agents/agents/dispatch.py"], issue_number=1
                )
        assert result.severity is VerifySeverity.FATAL
        assert "agents/agents/dispatch.py" in result.violating_paths

    def test_verify_py_is_fatal(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/agents/",)):
            with patch("agents.verify_checks.GHForge") as mock_gh:
                mock_gh.return_value.get_labels.return_value = []
                result = check_no_agent_infrastructure_changes(
                    tmp_path, ["agents/agents/verify.py"], issue_number=1
                )
        assert result.severity is VerifySeverity.FATAL
        assert "verify.py" in result.detail

    def test_persona_change_is_fatal(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/personas/",)):
            with patch("agents.verify_checks.GHForge") as mock_gh:
                mock_gh.return_value.get_labels.return_value = []
                result = check_no_agent_infrastructure_changes(
                    tmp_path, ["agents/personas/backend.md"], issue_number=1
                )
        assert result.severity is VerifySeverity.FATAL
        assert "agents/personas/backend.md" in result.violating_paths

    def test_workflow_change_is_fatal(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=(".github/workflows/",)):
            with patch("agents.verify_checks.GHForge") as mock_gh:
                mock_gh.return_value.get_labels.return_value = []
                result = check_no_agent_infrastructure_changes(
                    tmp_path, [".github/workflows/ci.yml"], issue_number=1
                )
        assert result.severity is VerifySeverity.FATAL
        assert ".github/workflows/ci.yml" in result.violating_paths

    def test_agents_tests_is_fatal(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/tests/",)):
            with patch("agents.verify_checks.GHForge") as mock_gh:
                mock_gh.return_value.get_labels.return_value = []
                result = check_no_agent_infrastructure_changes(
                    tmp_path, ["agents/tests/test_something.py"], issue_number=1
                )
        assert result.severity is VerifySeverity.FATAL

    def test_mixed_legit_plus_protected_is_fatal(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/agents/",)):
            with patch("agents.verify_checks.GHForge") as mock_gh:
                mock_gh.return_value.get_labels.return_value = []
                result = check_no_agent_infrastructure_changes(
                    tmp_path,
                    [
                        "frontend/src/App.tsx",
                        "agents/agents/verify.py",
                        "api/main.py",
                    ],
                    issue_number=1,
                )
        assert result.severity is VerifySeverity.FATAL
        assert "agents/agents/verify.py" in result.violating_paths
        assert "frontend/src/App.tsx" not in result.violating_paths
        assert "api/main.py" not in result.violating_paths

    def test_violating_paths_contains_all_protected_files(self, tmp_path):
        with patch("agents.verify_checks._get_protected_paths", return_value=("agents/agents/", ".github/workflows/")):
            with patch("agents.verify_checks.GHForge") as mock_gh:
                mock_gh.return_value.get_labels.return_value = []
                result = check_no_agent_infrastructure_changes(
                    tmp_path,
                    [
                        "agents/agents/dispatch.py",
                        ".github/workflows/ci.yml",
                        "frontend/src/App.tsx",
                    ],
                    issue_number=1,
                )
        assert result.severity is VerifySeverity.FATAL
        assert "agents/agents/dispatch.py" in result.violating_paths
        assert ".github/workflows/ci.yml" in result.violating_paths
        assert len(result.violating_paths) == 2


# ---------------------------------------------------------------------------
# check_integration_tests_for_chat_changes
# ---------------------------------------------------------------------------


class TestCheckIntegrationTestsForChatChanges:
    def test_no_chat_files_passes(self, tmp_path):
        result = check_integration_tests_for_chat_changes(
            tmp_path, ["frontend/src/App.tsx", "api/main.py"], issue_number=1
        )
        assert result.severity is VerifySeverity.OK
        assert result.passed is True

    def test_chat_file_with_streaming_test_passes(self, tmp_path):
        result = check_integration_tests_for_chat_changes(
            tmp_path,
            [
                "frontend/src/chat/ChatWidget.tsx",
                "frontend/src/__tests__/streaming.test.tsx",
            ],
            issue_number=1,
        )
        assert result.severity is VerifySeverity.OK

    def test_chat_file_with_widget_test_passes(self, tmp_path):
        result = check_integration_tests_for_chat_changes(
            tmp_path,
            [
                "frontend/src/chat/ChatWidget.tsx",
                "frontend/src/__tests__/widget.test.tsx",
            ],
            issue_number=1,
        )
        assert result.severity is VerifySeverity.OK

    def test_chat_file_no_streaming_test_is_retry(self, tmp_path):
        result = check_integration_tests_for_chat_changes(
            tmp_path,
            [
                "frontend/src/chat/ChatWidget.tsx",
                "frontend/src/__tests__/other.test.tsx",
            ],
            issue_number=1,
        )
        assert result.severity is VerifySeverity.RETRY
        assert result.passed is False

    def test_stream_file_no_test_is_retry(self, tmp_path):
        result = check_integration_tests_for_chat_changes(
            tmp_path,
            [
                "api/stream_handler.py",
            ],
            issue_number=1,
        )
        assert result.severity is VerifySeverity.RETRY

    def test_name_is_correct(self, tmp_path):
        result = check_integration_tests_for_chat_changes(tmp_path, [], issue_number=1)
        assert result.name == "chat_integration_tests"


# ---------------------------------------------------------------------------
# check_error_boundary_tests
# ---------------------------------------------------------------------------


class TestCheckErrorBoundaryTests:
    def test_tsx_without_error_boundary_passes(self, tmp_path):
        src = tmp_path / "frontend" / "src" / "App.tsx"
        src.parent.mkdir(parents=True)
        src.write_text("export const App = () => <div>Hello</div>;\n")

        result = check_error_boundary_tests(tmp_path, ["frontend/src/App.tsx"], issue_number=1)
        assert result.severity is VerifySeverity.OK

    def test_tsx_with_error_boundary_and_throw_test_passes(self, tmp_path):
        src = tmp_path / "frontend" / "src" / "Boundary.tsx"
        src.parent.mkdir(parents=True)
        src.write_text("<ErrorBoundary>...</ErrorBoundary>\n")

        test = tmp_path / "frontend" / "src" / "Boundary.test.tsx"
        test.write_text("it('catches errors', () => { throw new Error('boom'); });\n")

        result = check_error_boundary_tests(
            tmp_path,
            ["frontend/src/Boundary.tsx", "frontend/src/Boundary.test.tsx"],
            issue_number=1,
        )
        assert result.severity is VerifySeverity.OK

    def test_tsx_with_error_boundary_and_error_keyword_in_test_passes(self, tmp_path):
        src = tmp_path / "frontend" / "src" / "Boundary.tsx"
        src.parent.mkdir(parents=True)
        src.write_text("<ErrorBoundary>...</ErrorBoundary>\n")

        test = tmp_path / "frontend" / "src" / "Boundary.test.tsx"
        test.write_text("it('handles error state', () => { simulateError(); });\n")

        result = check_error_boundary_tests(
            tmp_path,
            ["frontend/src/Boundary.tsx", "frontend/src/Boundary.test.tsx"],
            issue_number=1,
        )
        assert result.severity is VerifySeverity.OK

    def test_tsx_with_error_boundary_no_throw_in_test_is_retry(self, tmp_path):
        src = tmp_path / "frontend" / "src" / "Boundary.tsx"
        src.parent.mkdir(parents=True)
        src.write_text("<ErrorBoundary>...</ErrorBoundary>\n")

        test = tmp_path / "frontend" / "src" / "Boundary.test.tsx"
        test.write_text("it('renders children', () => { render(<Boundary />); });\n")

        result = check_error_boundary_tests(
            tmp_path,
            ["frontend/src/Boundary.tsx", "frontend/src/Boundary.test.tsx"],
            issue_number=1,
        )
        assert result.severity is VerifySeverity.RETRY
        assert "frontend/src/Boundary.tsx" in result.violating_paths

    def test_tsx_with_error_boundary_missing_test_file_is_retry(self, tmp_path):
        src = tmp_path / "frontend" / "src" / "Boundary.tsx"
        src.parent.mkdir(parents=True)
        src.write_text("<ErrorBoundary>...</ErrorBoundary>\n")

        result = check_error_boundary_tests(
            tmp_path, ["frontend/src/Boundary.tsx"], issue_number=1
        )
        assert result.severity is VerifySeverity.RETRY

    def test_name_is_correct(self, tmp_path):
        result = check_error_boundary_tests(tmp_path, [], issue_number=1)
        assert result.name == "error_boundary_tests"

    def test_no_tsx_files_passes(self, tmp_path):
        result = check_error_boundary_tests(tmp_path, ["api/main.py"], issue_number=1)
        assert result.severity is VerifySeverity.OK


# ---------------------------------------------------------------------------
# check_tool_coverage_on_removal
# ---------------------------------------------------------------------------


class TestCheckToolCoverageOnRemoval:
    def test_no_tools_removed_passes(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("def add_tool(): pass\n")

        result = check_tool_coverage_on_removal(tmp_path, ["module.py"], issue_number=1)
        assert result.severity is VerifySeverity.OK
        assert result.passed is True

    def test_tool_removal_without_benchmarks_is_retry(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("tools.remove('old_tool')\n")

        result = check_tool_coverage_on_removal(tmp_path, ["module.py"], issue_number=1)
        assert result.severity is VerifySeverity.RETRY
        assert "benchmarks.json" in result.detail

    def test_tool_removal_with_benchmarks_passes(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("tools.remove('old_tool')\n")

        benchmarks = tmp_path / "agents" / "benchmarks.json"
        benchmarks.parent.mkdir(parents=True)
        benchmarks.write_text('{"benchmarks": []}')

        result = check_tool_coverage_on_removal(tmp_path, ["module.py"], issue_number=1)
        assert result.severity is VerifySeverity.OK

    def test_skips_verify_py(self, tmp_path):
        src = tmp_path / "agents" / "agents" / "verify.py"
        src.parent.mkdir(parents=True)
        src.write_text("Remove the tool from the list\n")

        result = check_tool_coverage_on_removal(
            tmp_path, ["agents/agents/verify.py"], issue_number=1
        )
        assert result.severity is VerifySeverity.OK

    def test_py_file_with_neither_remove_nor_tool_passes(self, tmp_path):
        src = tmp_path / "service.py"
        src.write_text("def add_feature(): pass\nx = compute()\n")

        result = check_tool_coverage_on_removal(tmp_path, ["service.py"], issue_number=1)
        assert result.severity is VerifySeverity.OK

    def test_name_is_correct(self, tmp_path):
        result = check_tool_coverage_on_removal(tmp_path, [], issue_number=1)
        assert result.name == "tool_coverage"

    def test_empty_file_list_passes(self, tmp_path):
        result = check_tool_coverage_on_removal(tmp_path, [], issue_number=1)
        assert result.severity is VerifySeverity.OK
