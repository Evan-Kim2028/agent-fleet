"""SilphCo-specific verification checks.

Silphco-project checks that are composed with the generic fleet checks in
agents/agents/verify.py. The verifier-attack tripwire
(check_no_agent_infrastructure_changes) lives here and returns FATAL, which
causes run_checks() to short-circuit immediately.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from agent_fleet.contracts.verify_result import VerifySeverity
from agent_fleet.integrations.github_forge import GitHubForge
from agent_fleet.repo import find_repo_config
from agent_fleet.verify_core import Check, CheckResult


@dataclass(frozen=True)
class VerifySettings:
    protected_paths: tuple[str, ...]
    override_label: str
    revoke_label: str
    verifier_escape_label: str
    critical_path_prefixes: tuple[str, ...]
    secrets_patterns: tuple[str, ...]


_DEFAULT_VERIFY = VerifySettings(
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


@lru_cache(maxsize=8)
def _load_verify_settings(worktree_path: str) -> VerifySettings:
    """Load verify tripwire settings from repo-root .agent-fleet.yaml."""
    repo = find_repo_config(Path(worktree_path))
    if repo is None:
        return _DEFAULT_VERIFY

    config_path = repo.repo_root / ".agent-fleet.yaml"
    if not config_path.exists():
        return VerifySettings(
            protected_paths=_DEFAULT_VERIFY.protected_paths,
            override_label=_DEFAULT_VERIFY.override_label,
            revoke_label=_DEFAULT_VERIFY.revoke_label,
            verifier_escape_label=_DEFAULT_VERIFY.verifier_escape_label,
            critical_path_prefixes=repo.critical_path_prefixes or _DEFAULT_VERIFY.critical_path_prefixes,
            secrets_patterns=_DEFAULT_VERIFY.secrets_patterns,
        )

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    verify = raw.get("verify") if isinstance(raw, dict) else {}
    if not isinstance(verify, dict):
        verify = {}

    protected = verify.get("protected_paths")
    protected_paths = (
        tuple(str(p) for p in protected)
        if isinstance(protected, list)
        else _DEFAULT_VERIFY.protected_paths
    )

    secrets = verify.get("secrets_patterns")
    secrets_patterns = (
        tuple(str(p) for p in secrets)
        if isinstance(secrets, list)
        else _DEFAULT_VERIFY.secrets_patterns
    )

    critical = repo.critical_path_prefixes or _DEFAULT_VERIFY.critical_path_prefixes

    return VerifySettings(
        protected_paths=protected_paths,
        override_label=str(verify.get("override_label") or _DEFAULT_VERIFY.override_label),
        revoke_label=str(verify.get("revoke_label") or _DEFAULT_VERIFY.revoke_label),
        verifier_escape_label=str(
            verify.get("verifier_escape_label") or _DEFAULT_VERIFY.verifier_escape_label
        ),
        critical_path_prefixes=critical,
        secrets_patterns=secrets_patterns,
    )


def _github_forge(worktree_path: Path) -> GitHubForge:
    return GitHubForge(cwd=worktree_path)


def _read_file(worktree_path: Path, rel_path: str) -> str:
    try:
        return (worktree_path / rel_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def check_no_agent_infrastructure_changes(
    worktree_path: Path, files: list[str], issue_number: int
) -> CheckResult:
    """Verifier-attack tripwire.

    Returns severity=FATAL if the diff touches protected fleet paths,
    UNLESS the issue carries the override label. If the revoke label is
    present, returns FATAL immediately (no retry — agent is explicitly blocked).
    """
    protected_paths = _load_verify_settings(str(worktree_path)).protected_paths
    violations = [
        f for f in files if any(f.startswith(p) for p in protected_paths)
    ]
    if not violations:
        return CheckResult(
            name="no_agent_infrastructure_changes",
            severity=VerifySeverity.OK,
        )

    settings = _load_verify_settings(str(worktree_path))
    labels = _github_forge(worktree_path).get_labels(issue_number)

    revoke_label = settings.revoke_label
    if revoke_label in labels:
        return CheckResult(
            name="no_agent_infrastructure_changes",
            severity=VerifySeverity.FATAL,
            detail=(
                f"PR has '{revoke_label}' label — agent merge rights revoked. "
                "Do not retry; manual review required."
            ),
            violating_paths=tuple(violations),
        )

    override_label = settings.override_label
    if override_label in labels:
        return CheckResult(
            name="no_agent_infrastructure_changes",
            severity=VerifySeverity.OK,
            detail=(
                f"Override label '{override_label}' present — "
                "protected path check bypassed by operator intent."
            ),
        )

    return CheckResult(
        name="no_agent_infrastructure_changes",
        severity=VerifySeverity.FATAL,
        detail=(
            "Agent modified protected infrastructure paths: "
            f"{violations}. These files run from the dispatcher / CI, "
            "not the worktree, and must not be modified by agents. "
            "If the human operator wants these changed, apply the "
            f"'{override_label}' label to this issue."
        ),
        violating_paths=tuple(violations),
    )


def check_no_verifier_self_modify(
    worktree_path: Path, files: list[str], issue_number: int
) -> CheckResult:
    """Hard tripwire: agents may NOT modify the verifier or dispatcher.

    This check runs *before* ``check_no_agent_infrastructure_changes`` and
    uses a separate, stricter escape hatch (``agent-can-modify-verifier``).
    The goal is to prevent the "verifier-attack" pattern observed in PR #812
    where an agent, failing verify, edited ``verify.py`` to silence the check
    rather than fix the code.

    Returns severity=FATAL if the diff touches any file under ``agents/agents/``
    or ``agents/silphco/`` (per ``SpineConfig.fleet_critical_prefixes``), UNLESS
    the issue carries the ``agent-can-modify-verifier`` label.
    """
    settings = _load_verify_settings(str(worktree_path))
    critical_prefixes = settings.critical_path_prefixes
    violations = [f for f in files if any(f.startswith(p) for p in critical_prefixes)]
    if not violations:
        return CheckResult(name="no_verifier_self_modify", severity=VerifySeverity.OK)

    labels = _github_forge(worktree_path).get_labels(issue_number)
    escape_label = settings.verifier_escape_label
    if escape_label in labels:
        return CheckResult(
            name="no_verifier_self_modify",
            severity=VerifySeverity.OK,
            detail=(
                f"Escape label '{escape_label}' present — "
                "verifier self-modification allowed by operator intent."
            ),
        )

    return CheckResult(
        name="no_verifier_self_modify",
        severity=VerifySeverity.FATAL,
        detail=(
            "Agent modified the verifier or dispatcher infrastructure. "
            f"This is forbidden. Revert the changes to: {violations}. "
            f"If this issue explicitly requires changing the verifier, "
            f"apply the '{escape_label}' label."
        ),
        violating_paths=tuple(violations),
    )


def check_integration_tests_for_chat_changes(
    worktree_path: Path, files: list[str], issue_number: int
) -> CheckResult:
    """Any change touching files with 'chat' or 'stream' in the path must
    also include a test file with 'stream' or 'widget' in its name.
    Returns RETRY on violation. Verbatim semantics from verify.py:186-205.
    """
    del worktree_path  # path not needed; check is purely on file list
    del issue_number  # unused but required by Check signature
    chat_files = [f for f in files if "chat" in f.lower() or "stream" in f.lower()]
    if not chat_files:
        return CheckResult(
            name="chat_integration_tests",
            severity=VerifySeverity.OK,
            detail="No chat files modified",
        )

    test_files = [f for f in files if "test" in f.lower()]
    has_streaming_test = any(
        "stream" in t.lower() or "widget" in t.lower() for t in test_files
    )

    if not has_streaming_test:
        return CheckResult(
            name="chat_integration_tests",
            severity=VerifySeverity.RETRY,
            detail=(
                f"Chat files modified ({chat_files}) but no streaming/widget "
                "integration test added"
            ),
        )
    return CheckResult(name="chat_integration_tests", severity=VerifySeverity.OK)


def check_error_boundary_tests(
    worktree_path: Path, files: list[str], issue_number: int
) -> CheckResult:
    """Any new ErrorBoundary in a .tsx file must have a corresponding test
    that throws/errors. Returns RETRY on violation. Verbatim from verify.py:208-236.
    """
    del issue_number  # unused but required by Check signature
    tsx_files = [f for f in files if f.endswith(".tsx")]
    test_files = [f for f in files if "test" in f.lower()]

    boundaries_without_tests: list[str] = []
    for src in tsx_files:
        content = _read_file(worktree_path, src)
        if "ErrorBoundary" not in content:
            continue

        base = src.rsplit(".", 1)[0]
        test_name = base + ".test.tsx"
        test_content = ""
        for t in test_files:
            if t.endswith(test_name.split("/")[-1]):
                test_content = _read_file(worktree_path, t)
                break

        if "throw" not in test_content and "error" not in test_content.lower():
            boundaries_without_tests.append(src)

    if boundaries_without_tests:
        return CheckResult(
            name="error_boundary_tests",
            severity=VerifySeverity.RETRY,
            detail=(
                f"ErrorBoundary added without error-triggering test: "
                f"{boundaries_without_tests}"
            ),
            violating_paths=tuple(boundaries_without_tests),
        )
    return CheckResult(name="error_boundary_tests", severity=VerifySeverity.OK)


def check_branch_sync(worktree_path: Path, files: list[str], issue_number: int) -> CheckResult:
    """Detect if the branch is behind origin/main and needs a rebase."""
    del files  # path not needed; check is purely on git state
    del issue_number  # unused but required by Check signature
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

    # Ensure we have the latest origin/main
    subprocess.run(
        ["git", "fetch", "origin", default_branch_name],
        cwd=worktree_path,
        capture_output=True,
        check=False,
    )

    merge_base_result = subprocess.run(
        ["git", "merge-base", f"origin/{default_branch_name}", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_base_result.returncode != 0:
        return CheckResult(
            name="branch_sync", severity=VerifySeverity.OK, detail="Could not determine merge-base"
        )

    merge_base = merge_base_result.stdout.strip()

    behind_result = subprocess.run(
        ["git", "rev-list", "--count", f"{merge_base}..origin/{default_branch_name}"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if behind_result.returncode != 0:
        return CheckResult(
            name="branch_sync", severity=VerifySeverity.OK, detail="Could not count commits behind"
        )

    behind_count = int(behind_result.stdout.strip() or "0")
    if behind_count > 0:
        return CheckResult(
            name="branch_sync",
            severity=VerifySeverity.RETRY,
            detail=(
                f"Branch is {behind_count} commit(s) behind origin/{default_branch_name}. "
                f"Run `git stash && git pull --rebase origin {default_branch_name} && git stash pop` "
                f"(or commit changes first, then `git pull --rebase origin {default_branch_name}`) "
                f"before proceeding."
            ),
        )
    return CheckResult(
        name="branch_sync", severity=VerifySeverity.OK, detail="Branch is up to date with origin/main"
    )


def check_tool_coverage_on_removal(
    worktree_path: Path, files: list[str], issue_number: int
) -> CheckResult:
    """If tool removal is detected (heuristic: 'remove' and 'tool' within
    40 chars in a non-verify.py file), require agents/benchmarks.json to
    exist. Returns RETRY on violation. Verbatim from verify.py:311-345.
    """
    del issue_number  # unused but required by Check signature
    py_files = [f for f in files if f.endswith(".py")]
    removed_tools: list[str] = []
    for f in py_files:
        # Skip the verification script itself — it talks about tools by design
        if f.endswith("verify.py"):
            continue
        content = _read_file(worktree_path, f)
        content_lower = content.lower()
        if "remove" in content_lower and "tool" in content_lower:
            for m in re.finditer(r"remove", content_lower):
                window = content_lower[max(0, m.start() - 40) : m.end() + 40]
                if "tool" in window:
                    removed_tools.append(f)
                    break

    if not removed_tools:
        return CheckResult(
            name="tool_coverage",
            severity=VerifySeverity.OK,
            detail="No tools removed",
        )

    # Check if benchmark file exists
    benchmark_path = worktree_path / "agents" / "benchmarks.json"
    if not benchmark_path.exists():
        return CheckResult(
            name="tool_coverage",
            severity=VerifySeverity.RETRY,
            detail=(
                f"Tools removed ({removed_tools}) but no benchmark file found at "
                "agents/benchmarks.json. Create benchmarks to verify remaining tools "
                "cover all user intents."
            ),
            violating_paths=tuple(removed_tools),
        )
    return CheckResult(name="tool_coverage", severity=VerifySeverity.OK)


def make_check_diff_respects_allowed_paths(allowed_paths: list[str]) -> Check:
    """Return a Check that validates changed files are within *allowed_paths*.

    An empty *allowed_paths* means no restriction (all files allowed).
    """

    def _check(worktree_path: Path, files: list[str], issue_number: int) -> CheckResult:
        del worktree_path  # path not needed; check is purely on file list
        del issue_number  # unused but required by Check signature
        if not allowed_paths:
            return CheckResult(
                name="diff_respects_allowed_paths",
                severity=VerifySeverity.OK,
                detail="No path restrictions for this persona",
            )
        violations = [
            f for f in files if not any(f.startswith(p) for p in allowed_paths)
        ]
        if violations:
            return CheckResult(
                name="diff_respects_allowed_paths",
                severity=VerifySeverity.RETRY,
                detail=(
                    f"Files changed outside persona scope: {violations}. "
                    f"Allowed prefixes: {allowed_paths}"
                ),
                violating_paths=tuple(violations),
            )
        return CheckResult(
            name="diff_respects_allowed_paths",
            severity=VerifySeverity.OK,
            detail="All changes within allowed paths",
        )

    return _check


def make_check_no_secrets_leaked(patterns: list[str]) -> Check:
    """Return a Check that scans changed files for secrets matching *patterns*.

    *patterns* are compiled as regexes. An empty list means the check is a no-op.
    """
    compiled = [re.compile(p) for p in patterns]

    def _check(worktree_path: Path, files: list[str], issue_number: int) -> CheckResult:
        del issue_number  # unused but required by Check signature
        if not compiled:
            return CheckResult(
                name="no_secrets_leaked",
                severity=VerifySeverity.OK,
                detail="No secret patterns configured",
            )
        violations: list[str] = []
        violating_paths: list[str] = []
        for f in files:
            # Only scan plausible text files
            if not f.endswith(
                (
                    ".py",
                    ".ts",
                    ".tsx",
                    ".js",
                    ".jsx",
                    ".json",
                    ".toml",
                    ".yaml",
                    ".yml",
                    ".md",
                    ".sh",
                    ".txt",
                )
            ):
                continue
            content = _read_file(worktree_path, f)
            if not content:
                continue
            for pat in compiled:
                if pat.search(content):
                    violations.append(f"{f}: matched {pat.pattern!r}")
                    violating_paths.append(f)
                    break
        if violations:
            return CheckResult(
                name="no_secrets_leaked",
                severity=VerifySeverity.RETRY,
                detail="Potential secrets found:\n" + "\n".join(violations[:10]),
                violating_paths=tuple(violating_paths),
            )
        return CheckResult(
            name="no_secrets_leaked",
            severity=VerifySeverity.OK,
            detail="No secrets patterns matched",
        )

    return _check
