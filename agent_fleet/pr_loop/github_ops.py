"""GitHub operations for PR loop (via gh CLI)."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_fleet.integrations.github_cli import gh as _gh
from agent_fleet.phases import run_scoped_lint_command

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommitPushResult:
    """Outcome of orchestrator commit + push (preflight, hooks, remote sync)."""

    ok: bool
    phase: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _git_run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    from agent_fleet.tool_env import augment_path

    if not Path(cwd).is_dir():
        return subprocess.CompletedProcess(
            args=args,
            returncode=128,
            stdout="",
            stderr=f"workspace does not exist: {cwd}",
        )
    run_env = augment_path(env)
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            env=run_env,
        )
    except FileNotFoundError as exc:
        cmd0 = args[0] if args else "<empty>"
        return subprocess.CompletedProcess(
            args=args,
            returncode=127,
            stdout="",
            stderr=f"command not found: {cmd0}: {exc}",
        )
    except NotADirectoryError as exc:
        return subprocess.CompletedProcess(
            args=args,
            returncode=128,
            stdout="",
            stderr=f"workspace vanished mid-run: {exc}",
        )


_FORBIDDEN_PATH_FRAGMENTS = (
    "/.venv/",
    "/.venv",
    "/node_modules/",
    "/__pycache__/",
    "/.pytest_cache/",
    "/.mypy_cache/",
    "/.ruff_cache/",
)


def _is_forbidden_path(path: str) -> bool:
    """True if *path* is a build/runtime artifact a fleet PR must never publish.

    `.venv` symlinks (or directories) accidentally staged by `git add -A`
    have poisoned merged PRs and broken self-hosted CI. We hard-block them
    here regardless of repo .gitignore state.
    """
    norm = "/" + path.lstrip("/")
    return any(
        frag in norm or norm.endswith(frag.rstrip("/")) for frag in _FORBIDDEN_PATH_FRAGMENTS
    )


def _changed_files(worktree: Path, *, exclude: tuple[str, ...] = ()) -> list[str]:
    if not Path(worktree).is_dir():
        return []
    exclude_set = set(exclude)
    # -uall expands untracked directories so per-file forbidden-path filters
    # can fire (a default `--porcelain` reports `pipeline/` for an entire
    # untracked dir, hiding a stray `.venv` inside).
    status = _git_run(["git", "status", "--porcelain", "-uall"], cwd=worktree, timeout=30)
    out: list[str] = []
    for line in status.stdout.splitlines():
        if not line.strip() or len(line) <= 3:
            continue
        path = line[3:].strip()
        if path in exclude_set or _is_forbidden_path(path):
            continue
        out.append(path)
    return out


def run_commit_preflight(
    worktree: Path,
    changed_files: list[str],
    commands: list[str],
) -> tuple[bool, str]:
    """Run repo verify/preflight commands and pre-commit on changed paths."""
    from agent_fleet.tool_env import ensure_pre_commit

    errors: list[str] = []

    precommit_cfg = worktree / ".pre-commit-config.yaml"
    if precommit_cfg.exists() and changed_files:
        pre_commit_bin = ensure_pre_commit(install=True)
        if not pre_commit_bin:
            errors.append(
                "pre-commit binary not found on PATH (and auto-install failed). "
                "Install with: uv tool install pre-commit"
            )
        else:
            pc = _git_run(
                [pre_commit_bin, "run", "--files", *changed_files],
                cwd=worktree,
                timeout=600,
            )
            if pc.returncode != 0:
                combined = "\n".join(part for part in (pc.stdout, pc.stderr) if part).strip()
                errors.append(combined[:4000] or "pre-commit failed")

    allowed_paths = tuple(changed_files)
    for command in commands:
        outcome = run_scoped_lint_command(
            worktree, command, timeout_s=600, allowed_paths=allowed_paths
        )
        if not outcome["passed"]:
            detail = outcome.get("detail") or outcome.get("stderr") or outcome.get("stdout") or ""
            errors.append(f"$ {command}\n{str(detail)[:2000]}")

    if errors:
        return False, "\n\n---\n\n".join(errors)
    return True, ""


def _sync_branch_before_push(worktree: Path, branch: str) -> tuple[bool, str]:
    fetch = _git_run(["git", "fetch", "origin", branch], cwd=worktree, timeout=120)
    fetch_output = (fetch.stderr or fetch.stdout or "").lower()
    if fetch.returncode != 0:
        if "couldn't find remote ref" in fetch_output:
            return True, ""
        return False, (fetch.stderr or fetch.stdout or "git fetch failed")[:500]

    remote = _git_run(["git", "rev-parse", f"origin/{branch}"], cwd=worktree, timeout=30)
    if remote.returncode != 0:
        return True, ""

    rebase = _git_run(["git", "rebase", f"origin/{branch}"], cwd=worktree, timeout=180)
    if rebase.returncode == 0:
        return True, ""

    _git_run(["git", "rebase", "--abort"], cwd=worktree, timeout=60)
    return False, (rebase.stderr or rebase.stdout or "git rebase failed")[:500]


def list_open_fleet_prs(
    *,
    branch_prefixes: tuple[str, ...],
    cwd: Path | None = None,
) -> list[dict[str, Any]]:
    result = _gh(
        "pr",
        "list",
        "--state",
        "open",
        "--json",
        "number,headRefName,headRefOid,baseRefName,labels,isDraft,mergeable,mergeStateStatus,createdAt",
        "--limit",
        "50",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return [
        pr
        for pr in prs
        if any(str(pr.get("headRefName", "")).startswith(prefix) for prefix in branch_prefixes)
    ]


def pr_comments(pr_number: int, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    result = _gh("pr", "view", str(pr_number), "--json", "comments", cwd=cwd, check=False)
    if result.returncode != 0:
        return []
    return json.loads(result.stdout).get("comments", [])


@dataclass(frozen=True)
class PrChecksSnapshot:
    """All buckets the merge gate cares about, with ignored checks called out
    separately so a debug log line can show ``failed=[...] ignored_failed=[...]``
    side-by-side. Knowing which check the loop saw as failing is the entire
    diagnostic when the merge gate refuses to merge a PR that GitHub considers
    mergeable.
    """

    all_filtered: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    failed: list[dict[str, Any]]
    ignored_failed: list[dict[str, Any]]


def pr_checks(
    pr_number: int,
    *,
    cwd: Path | None = None,
    ignored: tuple[str, ...] = (),
) -> PrChecksSnapshot:
    """Snapshot of CI checks for the merge gate.

    ``ignored`` names are removed from ``all_filtered`` (and therefore from
    ``pending`` / ``failed``). Any of those ignored checks that happened to be
    in ``bucket=fail`` are surfaced via ``ignored_failed`` for diagnostics —
    callers that want to log "we suppressed failure X" can read that field.
    """
    result = _gh(
        "pr",
        "checks",
        str(pr_number),
        "--json",
        "name,state,bucket",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return PrChecksSnapshot([], [], [], [])
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return PrChecksSnapshot([], [], [], [])

    ignored_set = {name.lower() for name in ignored}
    filtered = [check for check in checks if str(check.get("name", "")).lower() not in ignored_set]
    ignored_failed = [
        c
        for c in checks
        if str(c.get("name", "")).lower() in ignored_set and c.get("bucket") == "fail"
    ]
    pending = [c for c in filtered if c.get("bucket") == "pending"]
    failed = [c for c in filtered if c.get("bucket") == "fail"]
    return PrChecksSnapshot(filtered, pending, failed, ignored_failed)


def pr_changed_files(pr_number: int, *, cwd: Path | None = None) -> list[str]:
    result = _gh("pr", "view", str(pr_number), "--json", "files", cwd=cwd, check=False)
    if result.returncode != 0:
        return []
    files = json.loads(result.stdout).get("files", [])
    return [str(item.get("path", "")) for item in files if item.get("path")]




def pr_head_oid(pr_number: int, *, cwd: Path | None = None) -> str:
    """Return the PR head commit OID, or empty string on failure."""
    result = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "headRefOid",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    return str(payload.get("headRefOid") or "")

def pr_diff(pr_number: int, *, cwd: Path | None = None) -> str:
    result = _gh("pr", "diff", str(pr_number), cwd=cwd, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout


def pr_has_label(pr_number: int, label: str, *, cwd: Path | None = None) -> bool:
    result = _gh("pr", "view", str(pr_number), "--json", "labels", cwd=cwd, check=False)
    if result.returncode != 0:
        return False
    labels = json.loads(result.stdout).get("labels", [])
    return any(str(item.get("name", "")) == label for item in labels)


def pr_has_blocking_review(pr_number: int, *, cwd: Path | None = None) -> bool:
    result = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "reviewDecision,reviews",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return False
    payload = json.loads(result.stdout)
    if payload.get("reviewDecision") == "CHANGES_REQUESTED":
        return True
    for review in payload.get("reviews") or []:
        if review.get("state") == "CHANGES_REQUESTED":
            return True
    return False


def post_pr_comment(body: str, pr_number: int, *, cwd: Path | None = None) -> None:
    _gh("pr", "comment", str(pr_number), "--body", body, cwd=cwd)


def create_issue(
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
    cwd: Path | None = None,
) -> int | None:
    """Create a GitHub issue. Returns the issue number, or None on failure."""
    cmd = ["issue", "create", "--title", title, "--body", body]
    for label in labels or []:
        cmd.extend(["--label", label])
    result = _gh(*cmd, cwd=cwd, check=False)
    if result.returncode != 0:
        logger.warning("create_issue failed: %s", (result.stderr or "").strip()[:300])
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if "/issues/" in line:
            try:
                return int(line.rsplit("/", 1)[1])
            except ValueError:
                continue
    return None


def add_pr_label(pr_number: int, label: str, *, cwd: Path | None = None) -> None:
    _gh("label", "create", label, "--force", cwd=cwd, check=False)
    _gh("pr", "edit", str(pr_number), "--add-label", label, cwd=cwd, check=False)


def pr_is_draft(pr_number: int, *, cwd: Path | None = None) -> bool:
    result = _gh("pr", "view", str(pr_number), "--json", "isDraft", cwd=cwd, check=False)
    if result.returncode != 0:
        return False
    return bool(json.loads(result.stdout).get("isDraft"))


def _attempt_squash_merge(
    pr_number: int,
    *,
    subject: str,
    body: str,
    cwd: Path | None,
) -> subprocess.CompletedProcess[str]:
    return _gh(
        "pr",
        "merge",
        str(pr_number),
        "--squash",
        "--subject",
        subject,
        "--body",
        body,
        cwd=cwd,
        check=False,
    )


def _pr_is_behind_base(pr_number: int, *, cwd: Path | None) -> bool:
    """Detect 'PR branch is behind main' state via mergeStateStatus."""
    result = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "mergeStateStatus",
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return False
    status = json.loads(result.stdout).get("mergeStateStatus", "")
    return status == "BEHIND"


def update_branch(pr_number: int, *, cwd: Path | None = None) -> bool:
    """Server-side merge of base into the PR branch via `gh pr update-branch`.

    Returns True if GitHub accepted the request (branch was updated or already
    up to date). Returns False if there is a real merge conflict — only a human
    or the implementer agent can resolve that.
    """
    result = _gh("pr", "update-branch", str(pr_number), cwd=cwd, check=False)
    if result.returncode == 0:
        return True
    logger.warning("update-branch failed for PR #%s: %s", pr_number, result.stderr[:300])
    return False


def merge_pr(
    pr_number: int,
    *,
    subject: str,
    body: str,
    cwd: Path | None = None,
) -> bool:
    if pr_is_draft(pr_number, cwd=cwd):
        mark_pr_ready(pr_number, cwd=cwd)
    result = _attempt_squash_merge(pr_number, subject=subject, body=body, cwd=cwd)
    if result.returncode == 0:
        return True
    # Common mechanical failure: PR branch is behind main. Ask GitHub to merge
    # main into the PR branch server-side, then retry the squash merge.
    if _pr_is_behind_base(pr_number, cwd=cwd) and update_branch(pr_number, cwd=cwd):
        # update-branch triggers a new CI run; give it a moment and retry once.
        time.sleep(10)
        retry = _attempt_squash_merge(pr_number, subject=subject, body=body, cwd=cwd)
        if retry.returncode == 0:
            return True
    for _ in range(19):
        time.sleep(5)
        state_result = _gh("pr", "view", str(pr_number), "--json", "state", cwd=cwd, check=False)
        if state_result.returncode == 0:
            state = json.loads(state_result.stdout).get("state", "")
            if state == "MERGED":
                return True
    logger.warning("merge failed for PR #%s: %s", pr_number, result.stderr[:300])
    return False


def mark_pr_ready(pr_number: int, *, cwd: Path | None = None) -> None:
    _gh("pr", "ready", str(pr_number), cwd=cwd, check=False)


# ---------------------------------------------------------------------------
# Drift-detection helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeTreeResult:
    """Result of a git merge-tree dry-run between a branch and origin/main."""

    clean: bool
    """True when main merges into the branch without conflicts."""
    conflict_files: tuple[str, ...]
    """File paths that have conflict markers (empty when clean)."""
    git_error: bool = False
    """True when git itself failed (bad ref, wrong cwd, version mismatch, etc.).
    Callers must NOT treat this as a conflict."""


def merge_tree_against(base: str, head: str, *, cwd: Path) -> MergeTreeResult:
    """Dry-run merge of *base* into *head* without touching the working tree.

    Uses ``git merge-tree --write-tree --name-only <head> <base>`` (git >= 2.38).
    Exit code 0 means clean; exit code 1 means conflicts; any other exit code is
    a git error (bad ref, missing binary, etc.) and must NOT be treated as conflict.
    """
    result = _git_run(
        ["git", "merge-tree", "--write-tree", "--name-only", head, base],
        cwd=cwd,
        timeout=60,
    )
    if result.returncode == 0:
        return MergeTreeResult(clean=True, conflict_files=())
    if result.returncode == 1:
        conflict_files = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
        return MergeTreeResult(clean=False, conflict_files=conflict_files)
    # exit code > 1 → git error; do not treat as conflict
    logger.warning(
        "merge-tree exited %d (git error, not conflict): %s",
        result.returncode,
        (result.stderr or "").strip()[:300],
    )
    return MergeTreeResult(clean=False, conflict_files=(), git_error=True)


def is_pr_closed(pr_number: int, *, cwd: Path | None = None) -> bool:
    """Return True if the PR is in a closed or merged state on GitHub."""
    result = _gh("pr", "view", str(pr_number), "--json", "state", cwd=cwd, check=False)
    if result.returncode != 0:
        return False
    try:
        state = json.loads(result.stdout).get("state", "").upper()
        return state in {"CLOSED", "MERGED"}
    except Exception:
        return False


def close_pr(pr_number: int, *, cwd: Path | None = None) -> bool:
    """Close a PR without deleting the branch.  Safe if already closed.

    Returns True when the PR is now closed (including already-closed).
    Returns False on real errors (network, auth, etc.).
    "Already closed" is idempotent; "not found" is treated as already-closed and
    logged at WARNING so callers can retry safely.
    """
    result = _gh("pr", "close", str(pr_number), cwd=cwd, check=False)
    if result.returncode == 0:
        return True
    stderr_lower = (result.stderr or "").lower()
    if "already closed" in stderr_lower:
        return True
    if "not found" in stderr_lower:
        logger.warning("close_pr #%s: PR not found (treating as already closed)", pr_number)
        return True
    logger.warning("close_pr #%s failed: %s", pr_number, (result.stderr or "").strip()[:300])
    return False


def issue_comments(issue_number: int, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    """Return the comment list for a GitHub issue (not a PR)."""
    result = _gh("issue", "view", str(issue_number), "--json", "comments", cwd=cwd, check=False)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout).get("comments", [])
    except json.JSONDecodeError:
        return []


def reopen_issue(issue_number: int, *, cwd: Path | None = None) -> bool:
    """Reopen a closed issue.  Safe (no-op) if already open."""
    result = _gh("issue", "reopen", str(issue_number), cwd=cwd, check=False)
    if result.returncode == 0:
        return True
    stderr_lower = (result.stderr or "").lower()
    if "already open" in stderr_lower or "not found" in stderr_lower:
        return True
    logger.warning("reopen_issue #%s failed: %s", issue_number, (result.stderr or "").strip()[:300])
    return False


def post_issue_comment(body: str, issue_number: int, *, cwd: Path | None = None) -> None:
    """Post a comment on a GitHub issue."""
    _gh("issue", "comment", str(issue_number), "--body", body, cwd=cwd, check=False)


def checkout_branch(branch: str, worktree: Path, *, repo_root: Path) -> Path:
    from agent_fleet.pr_loop.worktree import (
        registered_worktree_for_branch,
        resolve_worktree_path,
    )

    registered = registered_worktree_for_branch(repo_root, branch)
    if registered is not None:
        worktree = registered
    elif not (
        worktree.exists() and ((worktree / ".git").exists() or (worktree / ".git").is_file())
    ):
        worktree = resolve_worktree_path(
            branch,
            repo_root=repo_root,
            worktree_base=worktree.parent,
        )

    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists() and ((worktree / ".git").exists() or (worktree / ".git").is_file()):
        subprocess.run(["git", "fetch", "origin", branch], cwd=worktree, check=True, timeout=120)
        subprocess.run(["git", "checkout", branch], cwd=worktree, check=False, timeout=60)
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=worktree,
            check=True,
            timeout=60,
        )
        return worktree

    subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=repo_root,
        check=True,
        timeout=120,
    )
    add = subprocess.run(
        ["git", "worktree", "add", "-B", branch, str(worktree), f"origin/{branch}"],
        cwd=repo_root,
        check=False,
        timeout=120,
    )
    if add.returncode != 0:
        existing = subprocess.run(
            ["git", "worktree", "list"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        for row in existing.stdout.splitlines():
            if branch in row:
                path = row.split()[0]
                subprocess.run(
                    ["git", "fetch", "origin", branch],
                    cwd=path,
                    check=True,
                    timeout=120,
                )
                subprocess.run(
                    ["git", "reset", "--hard", f"origin/{branch}"],
                    cwd=path,
                    check=True,
                    timeout=60,
                )
                return Path(path)
        add.check_returncode()
    return worktree


def _commits_ahead(worktree: Path, branch: str) -> int:
    ahead = _git_run(
        ["git", "rev-list", "--count", f"origin/{branch}..HEAD"],
        cwd=worktree,
        timeout=30,
    )
    if ahead.returncode != 0:
        return 0
    try:
        return int((ahead.stdout or "0").strip())
    except ValueError:
        return 0


def commit_and_push(
    worktree: Path,
    message: str,
    branch: str,
    *,
    exclude: tuple[str, ...] = (),
    preflight_commands: list[str] | None = None,
) -> CommitPushResult:
    if not Path(worktree).is_dir():
        return CommitPushResult(
            False,
            "no_workspace",
            f"worktree disappeared before publish: {worktree}",
        )
    changed = _changed_files(worktree, exclude=exclude)
    if not changed:
        _git_run(["git", "fetch", "origin", branch], cwd=worktree, timeout=120)
        if _commits_ahead(worktree, branch) <= 0:
            return CommitPushResult(
                False,
                "no_changes",
                "No staged or unstaged changes to commit",
            )
        synced, sync_detail = _sync_branch_before_push(worktree, branch)
        if not synced:
            return CommitPushResult(False, "push_failed", sync_detail)
        push = _git_run(
            ["git", "push", "origin", branch],
            cwd=worktree,
            timeout=180,
        )
        if push.returncode == 0:
            return CommitPushResult(True, "ok", "Pushed existing commit(s)")
        push_detail = (push.stderr or push.stdout or "git push failed").strip()
        return CommitPushResult(False, "push_failed", push_detail[:500])

    # Ensure pre-commit is on PATH before hooks / preflight (systemd hosts often
    # omit ~/.local/bin). Best-effort install when the repo uses pre-commit.
    if (worktree / ".pre-commit-config.yaml").exists():
        from agent_fleet.tool_env import ensure_pre_commit

        ensure_pre_commit(install=True)

    preflight_cmds = list(preflight_commands or [])
    if preflight_cmds:
        ok, detail = run_commit_preflight(worktree, changed, preflight_cmds)
        if not ok:
            logger.warning(
                "commit preflight failed on branch %s: %s",
                branch,
                detail[:300],
            )
            return CommitPushResult(False, "preflight_failed", detail)

    max_hook_retries = 2
    last_commit_output = ""
    for attempt in range(max_hook_retries + 1):
        # Re-scan after hooks (autofixers may add/remove files), filter
        # forbidden paths, and stage explicitly — never `git add -A` which
        # would pick up stray .venv / node_modules drops in the worktree.
        stage_paths = _changed_files(worktree, exclude=exclude)
        if not stage_paths:
            return CommitPushResult(False, "no_changes", "No publishable changes after filter")
        _git_run(
            ["git", "add", "-A", "--", *stage_paths],
            cwd=worktree,
            timeout=60,
            check=True,
        )
        commit = _git_run(
            ["git", "commit", "-m", message],
            cwd=worktree,
            timeout=120,
        )
        if commit.returncode == 0:
            break
        last_commit_output = (commit.stderr or commit.stdout or "").strip()
        post_status = _git_run(["git", "status", "--porcelain"], cwd=worktree, timeout=30)
        if not post_status.stdout.strip() or attempt == max_hook_retries:
            logger.warning(
                "commit failed on branch %s (attempt %d/%d): %s",
                branch,
                attempt + 1,
                max_hook_retries + 1,
                last_commit_output[:300],
            )
            return CommitPushResult(
                False,
                "commit_failed",
                last_commit_output or "git commit failed",
            )
        logger.info(
            "commit attempt %d/%d failed on branch %s (hook autofix?), retrying: %s",
            attempt + 1,
            max_hook_retries + 1,
            branch,
            last_commit_output[:200],
        )

    synced, sync_detail = _sync_branch_before_push(worktree, branch)
    if not synced:
        return CommitPushResult(False, "push_failed", sync_detail)

    for push_attempt in range(2):
        head = _git_run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree, timeout=30)
        on_branch = head.returncode == 0 and head.stdout.strip() == branch
        push_spec = branch if on_branch else f"HEAD:{branch}"
        push = _git_run(
            ["git", "push", "origin", push_spec],
            cwd=worktree,
            timeout=180,
        )
        if push.returncode == 0:
            return CommitPushResult(True, "ok")
        push_detail = (push.stderr or push.stdout or "git push failed").strip()
        non_ff = "non-fast-forward" in push_detail.lower()
        if push_attempt == 0 and non_ff:
            logger.info(
                "push non-fast-forward on %s; rebasing onto origin/%s and retrying",
                branch,
                branch,
            )
            synced, sync_detail = _sync_branch_before_push(worktree, branch)
            if not synced:
                return CommitPushResult(False, "push_failed", sync_detail)
            continue
        logger.warning("push failed: %s", push_detail[:300])
        return CommitPushResult(False, "push_failed", push_detail[:500])

    return CommitPushResult(False, "push_failed", "git push failed after retry")
