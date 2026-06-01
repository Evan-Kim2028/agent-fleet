"""Tests for the agent post-implement verification entrypoint (verify.py).

Covers CLI contract and composition smoke tests only. Per-check unit tests
live in fleet/tests/test_verify_core.py (generic checks) and
agents/test_verify_checks.py (silphco-specific checks).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# verify.py is in the same directory but not importable as a top-level module
# when pytest runs from the parent agents/ folder.
sys.path.insert(0, str(Path(__file__).parent))

from verify import (  # noqa: E402
    CHECKS,
    CheckResult,
    check_branch_sync,
    check_no_agent_infrastructure_changes,
    check_no_debug_code,
    check_tool_coverage_on_removal,
    check_type_checking_ran,
    get_changed_files,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo with one commit."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        capture_output=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# Back-compat re-export smoke tests
# ---------------------------------------------------------------------------


class TestBackCompatReexports:
    """Confirm that legacy 'from verify import X' still resolves."""

    def test_check_result_importable(self) -> None:
        assert CheckResult is not None

    def test_check_functions_importable(self) -> None:
        assert callable(check_no_agent_infrastructure_changes)
        assert callable(check_no_debug_code)
        assert callable(check_tool_coverage_on_removal)
        assert callable(check_type_checking_ran)
        assert callable(get_changed_files)


# ---------------------------------------------------------------------------
# TestMain — CLI smoke test
# ---------------------------------------------------------------------------


class TestMain:
    def test_exits_non_zero_on_missing_path(self) -> None:
        """main() returns 1 when worktree path does not exist."""
        original_argv = sys.argv[:]
        try:
            sys.argv = ["verify.py", "/nonexistent/path/xyz"]
            result = main()
        finally:
            sys.argv = original_argv
        assert result == 1

    def test_exits_non_zero_on_failure(self, tmp_path: Path) -> None:
        """Running main() against a worktree with a tripwire violation exits 1."""
        _init_git_repo(tmp_path)
        # Create a protected-path file to trigger the tripwire
        protected = tmp_path / "agents" / "agents" / "dispatch.py"
        protected.parent.mkdir(parents=True)
        protected.write_text("# modified\n")

        # Patch get_changed_files in fleet.verify_core so run_checks sees the file.
        import agent_fleet.verify_core as vc

        original = vc.get_changed_files
        try:
            vc.get_changed_files = lambda _p: ["agents/agents/dispatch.py"]
            original_argv = sys.argv[:]
            sys.argv = ["verify.py", str(tmp_path)]
            try:
                result = main()
            finally:
                sys.argv = original_argv
        finally:
            vc.get_changed_files = original

        assert result == 1


# ---------------------------------------------------------------------------
# TestComposition — CHECKS tuple order and integration behaviour
# ---------------------------------------------------------------------------


class TestComposition:
    def test_check_order_starts_with_tripwire(self) -> None:
        """CHECKS[0] is the FATAL tripwire, CHECKS[1] is branch_sync."""
        assert CHECKS[0] is check_no_agent_infrastructure_changes
        assert CHECKS[1] is check_branch_sync

    def test_run_with_protected_path_short_circuits(self, tmp_path: Path) -> None:
        """Touching agents/agents/dispatch.py => severity=fatal, len(checks)==1."""
        _init_git_repo(tmp_path)
        protected = tmp_path / "agents" / "agents" / "dispatch.py"
        protected.parent.mkdir(parents=True)
        protected.write_text("# agent modified this\n")

        import agent_fleet.verify_core as vc

        original = vc.get_changed_files

        import contextlib
        import io

        try:
            vc.get_changed_files = lambda _p: ["agents/agents/dispatch.py"]
            original_argv = sys.argv[:]
            sys.argv = ["verify.py", str(tmp_path)]
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    exit_code = main()
            finally:
                sys.argv = original_argv
        finally:
            vc.get_changed_files = original

        assert exit_code == 1
        output = buf.getvalue()
        data = json.loads(output)
        assert data["severity"] == "fatal"
        assert len(data["checks"]) == 1

    def test_run_with_clean_diff_returns_ok(self, tmp_path: Path) -> None:
        """A normal change with matching test exits 0."""
        _init_git_repo(tmp_path)

        # Create a frontend source file + matching test
        src = tmp_path / "frontend" / "src" / "App.tsx"
        src.parent.mkdir(parents=True)
        src.write_text("export const App = () => null;\n")

        test_dir = tmp_path / "frontend" / "src" / "__tests__"
        test_dir.mkdir(parents=True)
        (test_dir / "App.test.tsx").write_text(
            "import { App } from '../App';\ntest('renders', () => {});\n"
        )

        # Also add tsconfig.json so check_type_checking_ran passes
        (tmp_path / "tsconfig.json").write_text("{}")

        import agent_fleet.verify_core as vc

        original = vc.get_changed_files
        import io
        import contextlib

        try:
            vc.get_changed_files = lambda _p: [
                "frontend/src/App.tsx",
                "frontend/src/__tests__/App.test.tsx",
            ]
            original_argv = sys.argv[:]
            sys.argv = ["verify.py", str(tmp_path)]
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    exit_code = main()
            finally:
                sys.argv = original_argv
        finally:
            vc.get_changed_files = original

        assert exit_code == 0
