"""GitHub CLI helpers — all GitHub API ops via the local `gh` binary."""

import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path

import agents.logging as aglog

_repo_full_name: str = ""

# Inter-process push stagger. GitHub coalesces `pull_request`/`push` workflow
# triggers when the same actor pushes near-simultaneously to different refs —
# observed when two agent runs pushed 4s apart and one PR never registered a
# check-suite. Concurrent agent-dispatch processes serialize through this lock
# and sleep out the remainder of the window so each push lands as a distinct
# event upstream.
_PUSH_LOCK_PATH = "/tmp/agent-push.lock"
_PUSH_MIN_INTERVAL_SECONDS = 20.0


def set_repo(name: str) -> None:
    global _repo_full_name
    _repo_full_name = name


def _gh(
    *args, input_text: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if _repo_full_name:
        env["GH_REPO"] = _repo_full_name
    # Strip bot-scoped tokens so gh CLI uses its stored user PAT. When
    # `gh pr create` runs with GITHUB_TOKEN/GH_TOKEN set to a GitHub App
    # installation or Actions token, GitHub suppresses the resulting
    # `pull_request` workflow events to prevent recursion — that's why CI
    # never registered a check-suite for agent-opened PRs.
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
        input=input_text,
        env=env,
    )


def _user_token() -> str:
    """Return the gh-authenticated user's OAuth/PAT (gho_/ghp_)."""
    result = _gh("auth", "token", check=True)
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gh auth token returned empty — user must be logged in")
    return token


def _scrubbed_push_env() -> dict:
    """Env for git push that drops bot-scoped tokens so the credential helper wins."""
    env = {**os.environ}
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    return env


def _acquire_push_slot(min_interval: float = _PUSH_MIN_INTERVAL_SECONDS) -> int:
    """Block until the previous push is at least ``min_interval`` seconds old.

    Holds an exclusive flock on ``_PUSH_LOCK_PATH`` while sleeping so that
    parallel agent processes serialize, then stores the slot start timestamp
    inside the lock file so the *next* caller can compute its own wait. The
    caller must release the slot with :func:`_release_push_slot` *after* the
    push completes so the timestamp reflects when the push actually landed
    upstream, not when this process arrived.
    """
    fd = os.open(_PUSH_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode("ascii", errors="ignore").strip()
        last_push_ts = float(raw) if raw else 0.0
        wait = (last_push_ts + min_interval) - time.time()
        if wait > 0:
            aglog.log(
                "push-stagger", f"Holding push lock for {wait:.1f}s (next slot)..."
            )
            time.sleep(wait)
        return fd
    except Exception:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise


def _release_push_slot(fd: int) -> None:
    """Record the push completion time and release the lock."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{time.time():.3f}\n".encode("ascii"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# A `git commit` triggers the repo's pre-commit hook, which for frontend
# changes runs eslint over `frontend/` from a cold, uncached /tmp worktree
# (plus ruff/prettier/pyright on changed files). That routinely exceeds the
# original 30s budget, so healthy-but-slow commits were being killed mid-hook
# and the run aborted (#1180 re-dispatch d79268e3: "git commit timed out
# after 30s" on both the attempt and the retry). 300s comfortably covers a
# cold hook while still bounding a genuine hang.
_GIT_COMMIT_TIMEOUT_S = 300


def commit_with_hook_retry(
    worktree_path: Path | str,
    message: str,
    *,
    run_id: str = "",
    max_retries: int = 1,
) -> bool:
    """Run ``git commit -m message``, tolerating reformatting pre-commit hooks.

    Hooks that reformat (ruff format, prettier, ...) abort the commit with a
    non-zero exit and leave their edits in the working tree. Detect that case,
    re-stage with ``git add -A`` and retry the commit up to *max_retries* times.
    Returns ``True`` iff a commit was created. Never raises (timeouts and genuine
    failures return ``False``) so callers decide how to surface the failure
    instead of a bare ``CalledProcessError`` crashing the run (regression: #1180).

    *max_retries* defaults to 1 (legacy: one retry after the initial attempt) so
    existing callers that do not pass the argument are unaffected.  Fleet runner
    threads ``FleetConfig.max_commit_retries`` (default 2) through this parameter
    so the retry budget is configurable without touching every call site.

    On exhausting all retries the hook stderr from the last failing attempt is
    included in the log message so the operator can diagnose why the hook kept
    rejecting the commit.
    """
    cmd = ["git", "commit", "-m", message]
    tag = f"[{run_id}] " if run_id else ""
    last_stderr = ""
    try:
        result = subprocess.run(
            cmd,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_COMMIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        aglog.log("pr", f"{tag}git commit timed out after {_GIT_COMMIT_TIMEOUT_S}s")
        return False
    if result.returncode == 0:
        return True
    last_stderr = result.stderr or ""

    for attempt in range(max_retries):
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if not dirty.stdout.strip():
            aglog.log(
                "pr",
                f"{tag}git commit failed and working tree is clean — not retrying. "
                f"stderr={last_stderr[:500]}",
            )
            return False
        retry_num = attempt + 1
        aglog.log(
            "pr",
            f"{tag}git commit failed; working tree dirty (pre-commit reformat?), "
            f"re-staging and retrying (attempt {retry_num}/{max_retries}). "
            f"stderr={last_stderr[:500]}",
        )
        subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=False)
        try:
            retry = subprocess.run(
                cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_COMMIT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            aglog.log(
                "pr",
                f"{tag}git commit retry {retry_num} timed out after {_GIT_COMMIT_TIMEOUT_S}s",
            )
            return False
        if retry.returncode == 0:
            return True
        last_stderr = retry.stderr or ""

    aglog.log(
        "pr",
        f"{tag}git commit failed after {max_retries} retry/retries. "
        f"hook stderr={last_stderr[:500]}",
    )
    return False


# Match the GitHub token prefixes (`gho_` user OAuth, `ghp_` user PAT,
# `ghs_` server-to-server, `ghu_` user-to-server, `github_pat_` fine-grained).
# Keep this pattern conservative — over-matching is harmless, missing one
# would leak a credential to logs.
_TOKEN_REDACT_RE = re.compile(
    r"(?:gho_|ghp_|ghs_|ghu_|github_pat_)[A-Za-z0-9_]+"
)


def _redact_tokens(text: str) -> str:
    """Replace any GitHub token substring with ``<redacted>`` so callers can
    safely log error output. Subprocess stderr from ``git push -u`` includes
    the upstream-tracking URL, which embeds the user PAT and used to land
    in journald in plaintext."""
    if not text:
        return text
    return _TOKEN_REDACT_RE.sub("<redacted>", text)


def git_push_with_user_token(
    worktree_path: Path,
    branch_name: str,
    *,
    force_with_lease: bool = False,
    set_upstream: bool = False,
) -> None:
    """Push using the user PAT embedded in the URL.

    Guarantees the push event is attributed to a real user account (not a
    GitHub App installation token), so GitHub fires the `pull_request` /
    `push` workflows. Strips GITHUB_TOKEN/GH_TOKEN from the env and disables
    the credential helper to prevent silent override by inherited tokens.

    stdout/stderr are captured so git's upstream-tracking line (which
    contains the token-in-URL) does not stream to systemd journald. On
    non-zero exit the captured output is re-emitted with all GitHub tokens
    redacted via :func:`_redact_tokens`.
    """
    if not _repo_full_name:
        raise RuntimeError(
            "agents.github.set_repo() must be called before git_push_with_user_token"
        )

    token = _user_token()
    remote_url = f"https://x-access-token:{token}@github.com/{_repo_full_name}.git"

    cmd = ["git", "-c", "credential.helper=", "push"]
    if force_with_lease:
        cmd.append("--force-with-lease")
    if set_upstream:
        cmd.append("-u")
    cmd.extend([remote_url, branch_name])

    slot_fd = _acquire_push_slot()
    try:
        result = subprocess.run(
            cmd,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=False,
            env=_scrubbed_push_env(),
        )
        if result.returncode != 0:
            redacted_stderr = _redact_tokens(result.stderr or "")
            redacted_stdout = _redact_tokens(result.stdout or "")
            raise subprocess.CalledProcessError(
                result.returncode,
                # Scrub the URL out of the recorded args too — CalledProcessError
                # str() prints them and would leak the token otherwise.
                [a if a != remote_url else "<redacted-remote-url>" for a in cmd],
                redacted_stdout,
                redacted_stderr,
            )
    finally:
        _release_push_slot(slot_fd)


def wait_for_ci_trigger(
    head_sha: str, branch_name: str = "", *, attempts: int = 36, interval: int = 5
) -> bool:
    """Poll the commit's check-suites until github-actions creates one.

    Returns True if the github-actions app registered a check-suite within the
    poll window. False means CI was suppressed — almost always because the
    push credential was a GitHub App installation token, not a user PAT.
    """
    if not _repo_full_name:
        raise RuntimeError(
            "agents.github.set_repo() must be called before wait_for_ci_trigger"
        )

    for _ in range(attempts):
        result = _gh(
            "api",
            f"repos/{_repo_full_name}/commits/{head_sha}/check-suites",
            check=False,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                data = {}
            for suite in data.get("check_suites", []):
                app = suite.get("app") or {}
                if app.get("slug") == "github-actions":
                    return True
        time.sleep(interval)
    # CI check-suite never registered — attempt to trigger it manually.
    subprocess.run(
        ["gh", "workflow", "run", "ci.yml", "--ref", branch_name],
        check=False,
        capture_output=True,
    )
    return False


def gh_post_comment(issue_number: int, body: str) -> str | None:
    """Post a comment; return the numeric comment id if it can be parsed."""
    result = _gh("issue", "comment", str(issue_number), "--body", body)
    match = re.search(r"#issuecomment-(\d+)", result.stdout)
    return match.group(1) if match else None


def gh_update_comment(comment_id: str, body: str) -> None:
    """Edit an existing issue/PR comment in-place."""
    if not _repo_full_name:
        raise RuntimeError("gh_update_comment requires set_repo() to be called first")
    _gh(
        "api",
        "-X",
        "PATCH",
        f"/repos/{_repo_full_name}/issues/comments/{comment_id}",
        "-f",
        f"body={body}",
    )


def gh_ensure_label(label_name: str, color: str = "e11d48") -> None:
    _gh("label", "create", label_name, "--color", color, "--force", check=False)


def gh_add_label(issue_number: int, label_name: str) -> None:
    _gh("issue", "edit", str(issue_number), "--add-label", label_name)


def gh_remove_label(issue_number: int, label_name: str) -> None:
    _gh("issue", "edit", str(issue_number), "--remove-label", label_name, check=False)


def gh_issue_has_label(issue_number: int, label_name: str) -> bool:
    result = _gh("issue", "view", str(issue_number), "--json", "labels", check=False)
    if result.returncode != 0:
        return False
    data = json.loads(result.stdout)
    return any(lbl["name"] == label_name for lbl in data.get("labels", []))


def gh_pr_has_label(pr_number: int, label_name: str) -> bool:
    """Return True if the PR carries ``label_name``.

    Fail-safe: on any query error, return True. A false "label present"
    only withholds an automated merge (a human can still merge); a false
    "label absent" would let the merge race re-fire. We bias to the safe
    side.
    """
    result = _gh("pr", "view", str(pr_number), "--json", "labels", check=False)
    if result.returncode != 0:
        return True
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return True
    return any(lbl.get("name") == label_name for lbl in data.get("labels", []))


def gh_get_issue(issue_number: int) -> dict:
    result = _gh("issue", "view", str(issue_number), "--json", "title,body")
    return json.loads(result.stdout)


def gh_create_issue(title: str, body: str, labels: list[str] | None = None) -> str:
    """Open a new GitHub issue and return its URL.

    Labels that don't yet exist will fail the create — callers should
    ``gh_ensure_label`` first. The ``gh issue create`` command prints just the
    URL on success, which we return stripped of trailing whitespace.
    """
    cmd = ["issue", "create", "--title", title, "--body", body]
    for lbl in labels or []:
        cmd.extend(["--label", lbl])
    result = _gh(*cmd)
    return result.stdout.strip()


def gh_create_pr(
    branch: str, title: str, body: str, base: str = "main", draft: bool = False
) -> str:
    cmd = [
        "pr",
        "create",
        "--head",
        branch,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
    ]
    if draft:
        cmd.append("--draft")
    result = _gh(*cmd)
    return result.stdout.strip()


def gh_pr_comments(pr_number: int) -> list[dict]:
    result = _gh("pr", "view", str(pr_number), "--json", "comments", check=False)
    if result.returncode != 0:
        return []
    return json.loads(result.stdout).get("comments", [])


def gh_pr_checks(pr_number: int) -> list[dict]:
    # Fields: bucket=(fail|pass|pending|skipping), state=(FAILURE|SUCCESS|IN_PROGRESS|SKIPPED)
    result = _gh(
        "pr", "checks", str(pr_number), "--json", "name,state,bucket", check=False
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def gh_pr_merge(pr_number: int, subject: str, body: str) -> None:
    """Squash-merge a PR. Does NOT pass ``--delete-branch`` — that flag has
    ``gh`` run ``git branch -d`` in the parent repo, which fails noisily
    when the branch is still checked out in the agent's worktree (the
    worktree teardown runs after merge). Remote branch deletion is
    delegated to :func:`gh_delete_remote_branch`, which the caller should
    invoke once the worktree has been torn down.
    """
    import time as _time

    result = _gh(
        "pr",
        "merge",
        str(pr_number),
        "--squash",
        "--subject",
        subject,
        "--body",
        body,
        check=False,
    )
    if result.returncode == 0:
        return
    # Non-zero exit: gh pr merge returns 1 when branch-delete races or when the
    # merge itself completed but gh CLI lost the response. Poll the PR state for
    # up to 90s before raising — GitHub API propagation can lag by 30-60s.
    aglog.log(
        "github",
        f"gh pr merge exit {result.returncode}; stderr: {result.stderr[:300].strip()}",
    )
    for attempt in range(9):
        _time.sleep(10)
        state_result = _gh("pr", "view", str(pr_number), "--json", "state", check=False)
        if state_result.returncode == 0:
            state = json.loads(state_result.stdout).get("state", "")
            if state == "MERGED":
                aglog.log(
                    "github",
                    f"PR #{pr_number} confirmed MERGED after {(attempt + 1) * 10}s",
                )
                return
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


def gh_delete_remote_branch(branch_name: str) -> bool:
    """Delete ``refs/heads/<branch_name>`` on the configured remote repo.

    Returns ``True`` on success or when the ref is already gone. Returns
    ``False`` on transient gh failures so the caller can log without
    raising — leftover remote refs are tidy, not correctness.

    Called after :func:`gh_pr_merge` succeeds and the local worktree has
    been torn down, so neither side races a branch-delete against a still
    checked-out ref.
    """
    if not _repo_full_name:
        return False
    if not branch_name:
        return False
    result = _gh(
        "api",
        "-X",
        "DELETE",
        f"repos/{_repo_full_name}/git/refs/heads/{branch_name}",
        check=False,
    )
    if result.returncode == 0:
        return True
    # 422 "Reference does not exist" means someone else already deleted it.
    stderr = (result.stderr or "").lower()
    if "reference does not exist" in stderr or "not found" in stderr:
        return True
    aglog.log(
        "github",
        f"gh_delete_remote_branch({branch_name!r}) exit "
        f"{result.returncode}; stderr: {(result.stderr or '').strip()[:200]}",
    )
    return False


# git stderr fragments that indicate a *transient* concurrent-git failure
# (another fetch/dispatch holding a ref or index lock) rather than genuine
# repo corruption. These must NOT be treated as merge-integrity failures —
# doing so was the root cause of #1157 (good PRs auto-reverted on a raced
# `git fetch`/`git ls-tree`).
_GIT_TRANSIENT_MARKERS = (
    "cannot lock ref",
    "unable to create",
    ".lock",
    "another git process",
    "index.lock",
    "ref-lock",
    "could not lock",
    "is at",  # "is at <sha> but expected <sha>" — racey ref update
)


def _git_transient(stderr: str) -> bool:
    s = (stderr or "").lower()
    return any(m in s for m in _GIT_TRANSIENT_MARKERS)


def _git_with_retry(
    args: list[str], *, timeout: int, attempts: int = 3
) -> subprocess.CompletedProcess:
    """Run a git command, retrying on *transient* concurrent-git failures.

    Returns the last CompletedProcess. The caller distinguishes a genuine
    bad result (rc != 0, non-transient stderr) from a transient one via
    :func:`_git_transient` on the returned stderr.
    """
    result = subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=timeout
    )
    for attempt in range(1, attempts):
        if result.returncode == 0 or not _git_transient(result.stderr):
            break
        time.sleep(0.5 * attempt)
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=timeout
        )
    return result


def gh_validate_merge_integrity() -> list[str]:
    """Post-merge integrity check on origin/main.

    Fetches the latest default branch and inspects its tree for anomalies
    that indicate a corrupted merge — symlinks replacing directories, tracked
    files deleted, etc.  Returns a (possibly empty) list of error strings.

    Critically, this is **fail-open on inconclusive evidence**: a transient
    concurrent-git failure (ref/index lock race) is *not* corruption. When we
    cannot positively confirm the tree, we return ``[]`` rather than a false
    error, because the caller may act destructively (auto-revert) on a
    non-empty list. Only *confirmed* anomalies — a command that succeeded and
    showed a genuinely missing/symlinked path — are reported.
    """
    _timeout = 30
    errors: list[str] = []
    default_branch = gh_default_branch()

    fetch = _git_with_retry(
        ["git", "fetch", "origin", default_branch], timeout=_timeout
    )
    if fetch.returncode != 0:
        if _git_transient(fetch.stderr):
            # Inconclusive, not corrupt — do not let the caller revert.
            aglog.log(
                "github",
                "merge-integrity: post-merge fetch hit a transient git "
                f"lock race, treating as inconclusive: {fetch.stderr[:160]}",
            )
            return []
        return [f"post-merge fetch failed: {fetch.stderr[:200]}"]

    ref = f"origin/{default_branch}"

    # 1. data/ must be a directory tree (040000), not a symlink (120000).
    ls = _git_with_retry(["git", "ls-tree", ref, "data"], timeout=_timeout)
    if ls.returncode != 0:
        if _git_transient(ls.stderr):
            return []  # inconclusive
        errors.append(f"data/ ls-tree failed: {ls.stderr[:160]}")
    elif not ls.stdout.strip():
        errors.append("data/ missing from tree")
    else:
        mode = ls.stdout.split()[0]
        if mode == "120000":
            errors.append("data/ is a symlink — expected a directory tree")
        elif mode != "040000":
            errors.append(f"data/ has unexpected git mode: {mode}")

    # 2. Tracked data files must still exist.
    for path in (
        "data/card_index/jp_pokemon_name_to_dex.json",
        "data/card_index/set_pairs.json",
        "data/card_index/family_overrides.json",
    ):
        cat = _git_with_retry(
            ["git", "cat-file", "-e", f"{ref}:{path}"], timeout=_timeout
        )
        if cat.returncode != 0 and not _git_transient(cat.stderr):
            errors.append(f"tracked file missing: {path}")

    # 3. Scan the full tree for symlinks inside protected top-level dirs.
    protected = ("data/", "agents/", ".github/")
    tree = _git_with_retry(["git", "ls-tree", "-r", ref], timeout=_timeout)
    if tree.returncode == 0:
        for line in tree.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            mode_info, path = parts
            if mode_info.split()[0] == "120000" and path.startswith(protected):
                errors.append(f"symlink in protected path: {path}")

    return errors


def gh_pr_changed_files(pr_number: int) -> list[str]:
    """Return repo-relative file paths changed by the PR.

    Uses ``gh pr diff --name-only`` so no local worktree is required.
    Returns an empty list on any error (fail-open for the query itself;
    the caller's guard still blocks based on what we *can* see).
    """
    result = _gh("pr", "diff", str(pr_number), "--name-only", check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def gh_pr_diff(pr_number: int) -> str:
    """Return the unified diff for the PR (empty string on any error).

    Used by the deletion-only backstop in phases.py. Fail-open to "" — an
    empty diff makes ``_diff_is_deletion_only`` return False, i.e. the
    backstop does nothing and the normal blocking logic still applies.
    """
    result = _gh("pr", "diff", str(pr_number), check=False)
    if result.returncode != 0:
        return ""
    return result.stdout


def gh_revert_merge_commit(
    merge_sha: str,
    pr_number: int,
    repo_root: Path | None = None,
) -> bool:
    """Revert a merge commit on origin/main and push the revert.

    For squash merges, ``git revert -m 1 <sha>`` reverses the changes by
    keeping the first parent (main's previous state).  Returns True on
    success.  On conflict or push failure, aborts the revert and returns False.
    """
    _timeout = 30
    default_branch = gh_default_branch()
    cwd = repo_root or Path.cwd()

    # Fetch latest
    fetch = subprocess.run(
        ["git", "fetch", "origin", default_branch],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        timeout=_timeout,
    )
    if fetch.returncode != 0:
        aglog.log("revert", f"fetch failed: {fetch.stderr[:200]}")
        return False

    # Attempt revert
    revert = subprocess.run(
        ["git", "revert", "-m", "1", "--no-edit", merge_sha],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        timeout=_timeout,
    )
    if revert.returncode != 0:
        aglog.log(
            "revert",
            f"revert conflict or error: {revert.stdout[:300]} {revert.stderr[:200]}",
        )
        subprocess.run(
            ["git", "revert", "--abort"], cwd=cwd, check=False, timeout=_timeout
        )
        return False

    # Push the revert
    try:
        git_push_with_user_token(cwd, default_branch)
    except subprocess.CalledProcessError as exc:
        aglog.log("revert", f"push failed: {(exc.stderr or '')[:200]}")
        # Undo the local revert commit
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{default_branch}"],
            cwd=cwd,
            check=False,
            timeout=_timeout,
        )
        return False

    aglog.log(
        "revert", f"Successfully reverted merge {merge_sha[:8]} (PR #{pr_number})"
    )
    return True


def gh_default_branch() -> str:
    result = _gh("repo", "view", "--json", "defaultBranchRef", check=False)
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return data.get("defaultBranchRef", {}).get("name", "main")
    return "main"


_AGENT_PR_BRANCH_PREFIXES: tuple[str, ...] = ("agent/", "fleet/")


def gh_list_open_agent_prs() -> list[dict]:
    """Return open PRs whose head branch starts with 'agent/' or 'fleet/'.

    The legacy dispatch path creates ``agent/<persona>/...`` branches; the
    current fleet runner creates ``fleet/<persona>/...``. The auto-merge
    sweeper in ``agents.watcher._poll_agent_prs`` must see both.
    """
    result = _gh(
        "pr",
        "list",
        "--state",
        "open",
        "--json",
        "number,headRefName,labels,isDraft",
        "--limit",
        "50",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        prs = json.loads(result.stdout)
        return [
            pr
            for pr in prs
            if pr.get("headRefName", "").startswith(_AGENT_PR_BRANCH_PREFIXES)
        ]
    except json.JSONDecodeError:
        return []


def gh_list_merged_agent_prs(limit: int = 50) -> list[dict]:
    """Return recently MERGED PRs whose head branch starts with 'agent/' or 'fleet/'.

    Used by the coop merge gate to recognize cohort siblings that already
    shipped via a merged PR — even when the sibling issue stays open because
    the merged PR's body lacked the 'Closes #N' keyword (pre-#1011 runs).
    """
    result = _gh(
        "pr",
        "list",
        "--state",
        "merged",
        "--json",
        "number,headRefName",
        "--limit",
        str(limit),
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        prs = json.loads(result.stdout)
        return [
            pr
            for pr in prs
            if pr.get("headRefName", "").startswith(_AGENT_PR_BRANCH_PREFIXES)
        ]
    except json.JSONDecodeError:
        return []


def gh_list_open_issues_with_label(label: str) -> list[int]:
    """Return open issue numbers carrying ``label`` (PRs excluded).

    Used by the coop merge gate to discover cohort size: when a parent issue
    dispatches N child issues all labeled ``agent-coop-parent/<N>``, the
    watcher must hold any child PR's merge until every cohort sibling has
    a green PR — even ones that haven't opened a PR yet.
    """
    result = _gh(
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        label,
        "--json",
        "number",
        "--limit",
        "100",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        issues = json.loads(result.stdout)
        return [int(it["number"]) for it in issues if "number" in it]
    except (json.JSONDecodeError, ValueError, TypeError):
        return []


def gh_pr_mark_ready(pr_number: int) -> None:
    """Promote a draft PR to ready-for-review via ``gh pr ready``.

    Required before ``gh pr merge --squash`` — GitHub's GraphQL
    ``mergePullRequest`` rejects draft PRs with ``Pull Request is still a draft``.
    """
    _gh("pr", "ready", str(pr_number), check=False)


def gh_pr_update_branch(pr_number: int) -> bool:
    """Ask GitHub to merge the base branch into the PR branch (no local clone needed).

    Returns True if GitHub accepted the update request. A False return means the
    branch has unresolvable conflicts and needs manual intervention.
    """
    if not _repo_full_name:
        raise RuntimeError("agents.github.set_repo() must be called first")
    result = _gh(
        "api",
        f"repos/{_repo_full_name}/pulls/{pr_number}/update-branch",
        "--method",
        "PUT",
        check=False,
    )
    if result.returncode != 0:
        aglog.log(
            "github",
            f"update-branch PR #{pr_number} failed: {result.stderr[:200].strip()}",
        )
        return False
    return True


def gh_get_merge_state_status(pr_number: int) -> str | None:
    """Return the GitHub mergeStateStatus for a PR, or None on error.

    Values per GitHub GraphQL: CLEAN, DIRTY, BLOCKED, BEHIND, HAS_HOOKS,
    UNKNOWN, UNSTABLE, DRAFT. ``DIRTY`` means there is a merge conflict
    with the base branch — the case the auto-rebase loop watches for.
    """
    result = _gh(
        "pr",
        "view",
        str(pr_number),
        "--json",
        "mergeStateStatus",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout).get("mergeStateStatus") or None
    except json.JSONDecodeError:
        return None


def git_push_with_safe_rebase(
    worktree_path: Path,
    branch_name: str,
    *,
    force_with_lease: bool = False,
) -> tuple[bool, str | None]:
    """Push, and if non-fast-forward, fetch remote branch and rebase.

    Returns (success, error_reason). error_reason is None on success,
    or one of: ``'fetch_failed'``, ``'rebase_conflict'``, ``'push_failed'``.
    """
    try:
        git_push_with_user_token(
            worktree_path, branch_name, force_with_lease=force_with_lease
        )
        return True, None
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        if any(
            k in stderr
            for k in (
                "non-fast-forward",
                "fetch first",
                "stale info",
                " rejected ",
                "updates were rejected",
            )
        ):
            aglog.log(
                "push", "Push failed (non-fast-forward) — attempting safe rebase..."
            )

            fetch = subprocess.run(
                ["git", "fetch", "origin", branch_name],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if fetch.returncode != 0:
                aglog.log("push", f"Fetch failed: {fetch.stderr[:200]}")
                return False, "fetch_failed"

            rebase = subprocess.run(
                ["git", "rebase", f"origin/{branch_name}"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if rebase.returncode != 0:
                aglog.log("push", f"Rebase conflicts — aborting: {rebase.stdout[:300]}")
                subprocess.run(
                    ["git", "rebase", "--abort"], cwd=worktree_path, check=False
                )
                return False, "rebase_conflict"

            try:
                git_push_with_user_token(
                    worktree_path, branch_name, force_with_lease=True
                )
                aglog.log("push", "Rebased and pushed successfully.")
                return True, None
            except subprocess.CalledProcessError as exc2:
                aglog.log("push", f"Push failed even after rebase: {exc2}")
                return False, "push_failed"

        return False, "push_failed"


def gh_pr_has_blocking_review(pr_number: int) -> bool:
    """Return True if any reviewer currently has an active CHANGES_REQUESTED review."""
    result = _gh("pr", "view", str(pr_number), "--json", "reviews", check=False)
    if result.returncode != 0:
        return False
    try:
        reviews = json.loads(result.stdout).get("reviews", [])
        return any(r.get("state") == "CHANGES_REQUESTED" for r in reviews)
    except (json.JSONDecodeError, AttributeError):
        return False
