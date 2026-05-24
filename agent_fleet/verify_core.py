"""Generic verify helpers for the fleet engine."""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agent_fleet.contracts.verify_result import VerifyResult, VerifySeverity


@dataclass(frozen=True)
class CheckResult:
    """Result of a single verify check.

    severity=OK means the check passed; RETRY means a soft failure (the agent
    should fix and retry); FATAL means a tripwire was hit and the entire run
    is aborted immediately.
    """

    name: str
    severity: VerifySeverity
    detail: str = ""
    violating_paths: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.severity is VerifySeverity.OK


# A Check is any callable that takes (worktree_path, changed_files, issue_number) -> CheckResult.
Check = Callable[[Path, list[str], int], CheckResult]

DEFAULT_TEST_SEARCH_ROOTS: tuple[str, ...] = ("tests",)


def get_changed_files(worktree_path: Path) -> list[str]:
    """Return repo-relative paths of changed files introduced by this branch.

    Includes tracked-diffs (git diff against merge-base) and untracked files
    (git ls-files --others). Uses merge-base so that commits merged to main
    after the branch was created are not falsely attributed to the agent.
    """
    default_branch = (
        subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            check=False,
        )
        .stdout.strip()
        .replace("refs/heads/", "")
        or "main"
    )
    default_branch_name = default_branch.split("/")[-1]
    subprocess.run(
        ["git", "fetch", "origin", default_branch_name],
        cwd=worktree_path,
        capture_output=True,
        check=False,
    )

    # Use merge-base so we only see THIS branch's changes, not missing main commits.
    merge_base_result = subprocess.run(
        ["git", "merge-base", f"origin/{default_branch_name}", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    diff_target = merge_base_result.stdout.strip() or f"origin/{default_branch_name}"

    files: set[str] = set()
    diff_result = subprocess.run(
        ["git", "diff", "--name-only", diff_target, "--"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff_result.returncode == 0:
        files.update(diff_result.stdout.splitlines())

    untracked_result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if untracked_result.returncode == 0:
        files.update(untracked_result.stdout.splitlines())

    return sorted(f for f in files if f)


def is_git_repo(workspace: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def get_working_tree_changes(workspace: Path) -> list[str]:
    """Return repo-relative paths with uncommitted changes in *workspace*."""
    if not is_git_repo(workspace):
        return []

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []

    files: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            files.add(path)
    return sorted(files)


def get_working_tree_diff(workspace: Path, *, max_chars: int = 120_000) -> str:
    """Return unified diff for uncommitted changes (tracked + staged)."""
    if not is_git_repo(workspace):
        return ""

    parts: list[str] = []
    for args in (["diff", "HEAD"], ["diff", "--cached"]):
        result = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts.append(result.stdout)

    diff = "\n".join(parts).strip()
    if len(diff) <= max_chars:
        return diff
    return (
        diff[:max_chars]
        + f"\n\n... diff truncated at {max_chars} characters ..."
    )


def run_shell_verify(workspace: Path, command: str, *, timeout_s: int = 600) -> dict[str, object]:
    """Run a repo verify command and return a phase-shaped result dict."""
    result = subprocess.run(
        command,
        shell=True,
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )
    combined = (result.stdout or "") + (result.stderr or "")
    return {
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "passed": result.returncode == 0,
        "detail": combined[-4000:] if combined else "",
    }


def _check_result_to_dict(result: CheckResult) -> dict[str, object]:
    """Convert a CheckResult to the schema-compliant check entry dict."""
    return {
        "name": result.name,
        "passed": result.passed,
        "detail": result.detail,
    }


def run_checks(
    worktree_path: Path,
    checks: Iterable[Check],
    issue_number: int,
) -> VerifyResult:
    """Run *checks* in order against *worktree_path*.

    On the first FATAL CheckResult, short-circuit (tripwire): no further
    checks execute and the returned VerifyResult has severity=FATAL with only
    the violating check recorded.

    Otherwise: returns severity=RETRY if any non-FATAL check failed, OK if
    all passed.  Files-changed list is populated regardless.  Empty diff
    returns OK with empty checks.
    """
    files = get_changed_files(worktree_path)

    if not files:
        return VerifyResult(
            severity=VerifySeverity.OK,
            checks=[],
            violating_paths=[],
            files_changed=[],
            message="no changes",
        )

    check_list = list(checks)
    completed: list[CheckResult] = []
    any_retry = False

    for check in check_list:
        result = check(worktree_path, files, issue_number)
        if result.severity is VerifySeverity.FATAL:
            # Tripwire: short-circuit, record only this check.
            return VerifyResult(
                severity=VerifySeverity.FATAL,
                checks=[_check_result_to_dict(result)],
                violating_paths=list(result.violating_paths),
                files_changed=files,
                message=f"tripwire check failed: {result.name}",
            )
        completed.append(result)
        if result.severity is not VerifySeverity.OK:
            any_retry = True

    final_severity = VerifySeverity.RETRY if any_retry else VerifySeverity.OK
    violating = [
        path
        for r in completed
        for path in r.violating_paths
    ]
    message = "all checks passed" if not any_retry else "one or more checks failed"

    return VerifyResult(
        severity=final_severity,
        checks=[_check_result_to_dict(r) for r in completed],
        violating_paths=violating,
        files_changed=files,
        message=message,
    )


# ---------- helpers ----------


def _read_file(worktree_path: Path, rel_path: str) -> str:
    try:
        return (worktree_path / rel_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _test_file_exists(
    worktree_path: Path,
    src: str,
    *,
    test_search_roots: tuple[str, ...] = DEFAULT_TEST_SEARCH_ROOTS,
) -> bool:
    """Search for an existing test file that corresponds to *src*.

    Globs are constrained to test_search_roots so callers can limit the
    search scope (e.g. passing only ("tests",) skips custom_tests/).
    """
    src_path = Path(src)
    filename = src_path.stem
    suffix = src_path.suffix

    # Build glob patterns scoped to each test_search_root rather than
    # searching the entire worktree — prevents false positives from
    # unrelated directories that happen to match (e.g. custom_tests/).
    for root_name in test_search_roots:
        root = worktree_path / root_name
        if not root.exists():
            continue

        if suffix == ".py":
            py_patterns = [
                f"test_{filename}.py",
                f"test_{filename}_*.py",
                f"**/test_{filename}.py",
                f"**/{filename}_test.py",
            ]
            for pattern in py_patterns:
                if next(root.glob(pattern), None) is not None:
                    return True
        elif suffix in (".ts", ".tsx"):
            ts_patterns = [
                f"{filename}.test{suffix}",
                f"**/{filename}.test{suffix}",
            ]
            for pattern in ts_patterns:
                if next(root.glob(pattern), None) is not None:
                    return True

    # Content-based fallback: search test files for imports of this module.
    module_path = str(src_path.with_suffix("")).replace("/", ".")
    module_paths = [module_path]
    if "." in module_path:
        module_paths.append(module_path.split(".", 1)[1])

    for root_name in test_search_roots:
        root = worktree_path / root_name
        if not root.exists():
            continue
        for test_file in root.rglob("test_*"):
            if test_file.suffix not in (".py", ".ts", ".tsx"):
                continue
            try:
                content = test_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(mp in content for mp in module_paths):
                return True

    return False


# ---------- generic checks ----------


def check_tests_for_modified_code(
    worktree_path: Path,
    files: list[str],
    issue_number: int,
    *,
    test_search_roots: tuple[str, ...] = DEFAULT_TEST_SEARCH_ROOTS,
) -> CheckResult:
    del issue_number  # unused but required by Check signature
    """Every modified .py/.ts/.tsx file must have a corresponding test file.

    Generic version: silphco-specific search roots are passed via the
    test_search_roots kwarg.  Returns severity=RETRY on miss.
    """
    src_files = [
        f
        for f in files
        if f.endswith((".py", ".ts", ".tsx")) and "test" not in f.lower()
    ]
    test_files = [f for f in files if "test" in f.lower()]

    missing: list[str] = []
    for src in src_files:
        skip_markers = (
            "config",
            "types",
            ".d.ts",
            "stories",
            "mocks",
            "scripts/",
            "research/",
        )
        if any(x in src for x in skip_markers):
            continue

        # Skip private modules (Python convention: `_foo.py`). These are
        # internal implementation details — typically extracted from a public
        # module during a move-only refactor — and are tested transitively
        # through their public consumer's test file. `__init__.py` is excluded
        # since it's a package marker, not a private module.
        basename = src.rsplit("/", 1)[-1]
        if (
            basename.startswith("_")
            and not basename.startswith("__")
            and basename.endswith(".py")
        ):
            continue

        base = src.rsplit(".", 1)[0]
        # Note: `base.replace("/", "/tests/") + ".py"` is intentionally omitted
        # for root-level files (no "/" in base) to avoid treating the source file
        # itself as its own test.
        possible_tests = [
            base.replace("/src/", "/tests/") + ".test.py",
            base.replace("/src/", "/__tests__/") + ".test.tsx",
            base.replace("/src/", "/__tests__/") + ".test.ts",
            base + ".test.py",
            base + ".test.ts",
            base + ".test.tsx",
        ]
        if "/" in base:
            possible_tests.append(base.replace("/", "/tests/") + ".py")
            dir_part, file_part = base.rsplit("/", 1)
            possible_tests.append(f"{dir_part}/tests/test_{file_part}.py")
            # Colocated sibling __tests__/ (e.g. pages/card-detail/__tests__/Foo.test.tsx)
            possible_tests.append(f"{dir_part}/__tests__/{file_part}.test.ts")
            possible_tests.append(f"{dir_part}/__tests__/{file_part}.test.tsx")
            # Parent __tests__/ with submodule prefix (e.g. chartOptions.foo.test.ts)
            if "/" in dir_part:
                parent_dir, subdir_name = dir_part.rsplit("/", 1)
                possible_tests.append(f"{parent_dir}/__tests__/{subdir_name}.{file_part}.test.ts")
                possible_tests.append(f"{parent_dir}/__tests__/{subdir_name}.{file_part}.test.tsx")

        has_test = any(t in test_files for t in possible_tests)
        if not has_test:
            test_exists = any((worktree_path / t).exists() for t in possible_tests)
            if not test_exists:
                test_exists = _test_file_exists(
                    worktree_path, src, test_search_roots=test_search_roots
                )
            if not test_exists:
                missing.append(src)

    if missing:
        return CheckResult(
            name="tests_for_modified_code",
            severity=VerifySeverity.RETRY,
            detail=f"Modified source files without corresponding test changes: {missing}",
            violating_paths=tuple(missing),
        )
    return CheckResult(name="tests_for_modified_code", severity=VerifySeverity.OK)


def _python_debug_violations(content: str) -> list[tuple[int, str]]:
    """Use AST to find actual debug calls/statements in Python source.

    Flags only true debugger artifacts: ``import pdb`` and ``breakpoint()``.
    ``print()`` is intentionally not flagged.
    """
    violations: list[tuple[int, str]] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return violations

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pdb":
                    violations.append((node.lineno, "import pdb"))
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pdb":
                violations.append((node.lineno, "import pdb"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "breakpoint"
        ):
            violations.append((node.lineno, "breakpoint()"))
    return violations


def check_no_debug_code(worktree_path: Path, files: list[str], issue_number: int) -> CheckResult:
    del issue_number  # unused but required by Check signature
    """No console.log, debugger;, breakpoint(), or import pdb in non-test source.

    AST-based for Python (avoids string-literal false positives).
    Skips files ending in 'verify.py' (self-referential).
    TODO detection: flags ``# TODO`` / ``// TODO`` that are NOT ``TODO(name)``.
    Returns severity=RETRY on violation.
    """
    violations: list[str] = []
    violating_paths: list[str] = []

    for f in files:
        if not f.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
            continue
        if "test" in f.lower():
            continue
        if f.endswith("verify.py"):
            continue

        content = _read_file(worktree_path, f)
        lines = content.splitlines()
        file_violated = False

        if f.endswith(".py"):
            for lineno, detail in _python_debug_violations(content):
                violations.append(f"{f}:{lineno}: {detail}")
                file_violated = True
        else:
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("console.log("):
                    violations.append(f"{f}:{i}: {stripped[:60]}")
                    file_violated = True
                if "debugger;" in stripped:
                    violations.append(f"{f}:{i}: {stripped[:60]}")
                    file_violated = True

        # Detect TODO markers in all languages
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if re.search(r"#\s*TODO|//\s*TODO", line) and "TODO(" not in line:
                violations.append(f"{f}:{i}: {stripped[:60]}")
                file_violated = True

        if file_violated:
            violating_paths.append(f)

    if violations:
        return CheckResult(
            name="no_debug_code",
            severity=VerifySeverity.RETRY,
            detail="Debug/TODO code found:\n" + "\n".join(violations[:10]),
            violating_paths=tuple(violating_paths),
        )
    return CheckResult(name="no_debug_code", severity=VerifySeverity.OK)


def _has_pyright_config(path: Path) -> bool:
    if (path / "pyrightconfig.json").exists():
        return True
    toml = path / "pyproject.toml"
    if toml.exists():
        try:
            text = toml.read_text(encoding="utf-8")
            if "[tool.pyright" in text or "[tool.mypy" in text:
                return True
        except (OSError, UnicodeDecodeError):
            pass
    return False


def _has_ruff_config(path: Path) -> bool:
    toml = path / "pyproject.toml"
    if toml.exists():
        try:
            text = toml.read_text(encoding="utf-8")
            if "[tool.ruff" in text:
                return True
        except (OSError, UnicodeDecodeError):
            pass
    return False


def _has_tsconfig(path: Path) -> bool:
    return (
        (path / "tsconfig.json").exists()
        or next(path.rglob("tsconfig.json"), None) is not None
    )


def check_type_checking_ran(
    worktree_path: Path, files: list[str], issue_number: int
) -> CheckResult:
    del issue_number  # unused but required by Check signature
    """Heuristic: if .py changed, require pyright/mypy/ruff config in tree.
    If .ts/.tsx changed, require tsconfig.json.
    Returns severity=RETRY on violation. Verbatim semantics from verify.py:348-406.
    """
    has_pyright = _has_pyright_config(worktree_path) or any(
        _has_pyright_config(d)
        for d in worktree_path.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not has_pyright:
        has_pyright = _has_ruff_config(worktree_path) or any(
            _has_ruff_config(d)
            for d in worktree_path.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    has_tsconfig = _has_tsconfig(worktree_path)

    frontend_changed = any(f.endswith((".ts", ".tsx")) for f in files)
    backend_changed = any(f.endswith(".py") for f in files)

    if frontend_changed and not has_tsconfig:
        return CheckResult(
            name="type_checking",
            severity=VerifySeverity.RETRY,
            detail="Frontend files changed but no tsconfig.json found",
        )
    if backend_changed and not has_pyright:
        return CheckResult(
            name="type_checking",
            severity=VerifySeverity.RETRY,
            detail="Python files changed but no pyright config found",
        )

    return CheckResult(name="type_checking", severity=VerifySeverity.OK)
