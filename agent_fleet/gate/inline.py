"""Inlining a PR's change into a prompt for backends that cannot read the repo.

Some gate roles run on a remote backend that the pipeline does not let loose in
the worktree with a shell. For those, the evidence the reviewer needs is pasted
into the prompt instead of being fetched: the PR diff plus the full post-change
content of the changed non-test files.

Two properties make that safe to rely on:

**Bounded.** Every part is capped (diff, per-file, total). When the total cap is
reached the remaining files are *dropped* and the omission is stated in the
context footer — a reviewer must never be silently handed a partial view and
report it as complete, because the gate cannot tell a truncated review from a
clean one.

**Complete for what it claims.** The diff is included whole (up to its own cap)
because a half diff invents phantom context: a deleted line is only meaningful
next to the line that replaced it. Per-file content is a convenience over the
diff, and it is what gets dropped first.

Test files are excluded from the content slice. The gate already *runs* the
changed tests (step0) and has verifiers write against them; a lens looking for a
logic defect is not served by a paste of the test file it is judging.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from agent_fleet.gate.gitops import resolve_diff_base
from agent_fleet.gate.pytest_runner import is_test_file

logger = logging.getLogger(__name__)

DEFAULT_DIFF_CHARS = 60_000
DEFAULT_FILE_CHARS = 20_000
DEFAULT_TOTAL_CHARS = 120_000


@dataclass(frozen=True)
class FileSlice:
    """One changed file's post-change content, capped."""

    path: str
    text: str
    truncated: bool = False

    def render(self) -> str:
        body = self.text if not self.truncated else f"{self.text}\n... [truncated]"
        return f"----- FILE: {self.path} -----\n{body}\n----- END FILE -----"


@dataclass(frozen=True)
class ReviewContext:
    """The change, rendered as prompt text, plus what was left out.

    ``omitted`` is the fail-visible part: a non-zero value means the reviewer is
    looking at a subset of the change and the prompt says so.
    """

    diff: str = ""
    files: tuple[FileSlice, ...] = ()
    omitted: int = 0
    diff_truncated: bool = False
    skipped_test_files: tuple[str, ...] = field(default_factory=tuple)
    unavailable: str = ""
    degraded: str = ""

    def is_empty(self) -> bool:
        return not self.diff and not self.files

    def render(self) -> str:
        """The full context block, ready to concatenate into a prompt."""
        if self.unavailable:
            # Not "(no change detected)": that placeholder is indistinguishable
            # from a clean PR, and a reviewer handed it under a "the change
            # under review" header would report a review of code it never saw.
            return f"[REVIEW CONTEXT UNAVAILABLE: {self.unavailable}]"
        if self.is_empty():
            return "(no change detected)"
        parts: list[str] = []
        if self.diff:
            diff_body = self.diff
            if self.diff_truncated:
                diff_body = f"{self.diff}\n... [diff truncated]"
            # Deliberately NOT labelled with the command that produced it: this
            # text is read by a backend with no shell, and a "run git diff"
            # looking header invites it to try.
            parts.append(
                f"----- DIFF (the change under review) -----\n{diff_body}\n----- END DIFF -----"
            )
        parts.extend(f.render() for f in self.files)
        notes: list[str] = []
        if self.degraded:
            notes.append(self.degraded)
        if self.omitted:
            notes.append(f"{self.omitted} further changed file(s) were omitted by the size cap")
        if self.skipped_test_files:
            notes.append(
                "test files changed (not pasted; their content is not review evidence "
                "here): " + ", ".join(self.skipped_test_files)
            )
        if notes:
            parts.append("----- NOTE -----\n" + "\n".join(notes) + "\n----- END NOTE -----")
        return "\n\n".join(parts)


def _read_text(root: Path, rel: str) -> str | None:
    """Read one changed file's post-change content, or None if it is not safe to.

    The path is assembled from ``git diff --name-only`` output, so it is chosen by
    the PR — and it may be a symlink pointing anywhere on the host. This text is
    transmitted to a third-party backend, so the file read must be a *regular*
    file contained in the worktree: ``is_symlink()`` rejects the link itself, and
    the resolved-parent check rejects one that escapes via an intermediate
    directory symlink.
    """
    target = root / rel
    if target.is_symlink():
        logger.warning("gate: not inlining %s for prompt inlining: it is a symlink", rel)
        return None
    try:
        if not target.is_file():
            return None
        resolved = target.resolve(strict=True)
    except OSError as exc:
        logger.warning("gate: could not resolve %s for prompt inlining: %s", rel, exc)
        return None
    if not resolved.is_relative_to(root):
        logger.warning("gate: not inlining %s for prompt inlining: it escapes the worktree", rel)
        return None
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # A single unreadable file must not lose the whole review.
        logger.warning("gate: could not read %s for prompt inlining: %s", rel, exc)
        return None


def build_review_context(
    worktree: Path,
    base_branch: str,
    *,
    max_diff_chars: int = DEFAULT_DIFF_CHARS,
    max_file_chars: int = DEFAULT_FILE_CHARS,
    max_total_chars: int = DEFAULT_TOTAL_CHARS,
) -> ReviewContext:
    """Render the PR change at *worktree* for a backend that cannot read files.

    *base_branch* is resolved to ``origin/<branch>`` exactly as the lens prompt
    does, so the pasted diff is the same diff the reviewer would have run.
    """
    root = Path(worktree)
    diff_base = resolve_diff_base(root, base_branch)
    range_spec = f"{diff_base}...HEAD"

    def _diff(*args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", "-C", str(root), "diff", *args],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("gate: git diff %s failed for prompt inlining: %s", args, exc)
            return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr=str(exc))

    # A diff that cannot be computed is NOT a PR with no changes. git reports an
    # unresolvable range (no merge base, wrong base ref) on stderr with nothing on
    # stdout, so swallowing the exit code would hand the reviewer the empty
    # placeholder as "the change under review" and record a clean review of code
    # nobody saw. Retry the two-dot form, and if that fails too, fail visibly.
    done = _diff(range_spec)
    resolved_range = range_spec
    degraded = ""
    if done.returncode != 0:
        retry = _diff(f"{diff_base}..HEAD")
        if retry.returncode == 0:
            done = retry
            resolved_range = f"{diff_base}..HEAD"
            # The merge-base range is the one the reviewer would have run. The
            # two-dot form is a strictly wider diff (it also shows what the base
            # moved), so say so rather than let the reviewer assume it reviewed
            # exactly the PR's change.
            degraded = (
                f"the merge-base diff against {diff_base} is unavailable (no merge "
                f"base with HEAD), so this is the wider '{diff_base}..HEAD' diff and "
                "it may include changes that are not part of this PR"
            )
        else:
            lines = (retry.stderr or retry.stdout or "").strip().splitlines()
            detail = lines[0][:200] if lines else f"git diff exited {retry.returncode}"
            logger.warning("gate: could not diff for prompt inlining: %s", detail)
            return ReviewContext(unavailable=detail)

    diff = done.stdout or ""
    diff_truncated = False
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars]
        diff_truncated = True

    # The changed-file set is derived from the range that actually resolved, so
    # the pasted files and the pasted diff can never describe different ranges.
    names = _diff("--name-only", resolved_range)
    changed = [line.strip() for line in (names.stdout or "").splitlines() if line.strip()]
    test_files = [p for p in changed if is_test_file(p)]
    source_files = [p for p in changed if not is_test_file(p)]

    files: list[FileSlice] = []
    omitted = 0
    # The diff is the mandatory part, so it is charged against the total first.
    budget = max(0, max_total_chars - len(diff))
    for rel in source_files:
        text = _read_text(root, rel)
        if text is None:
            continue
        truncated = len(text) > max_file_chars
        body = text[:max_file_chars] if truncated else text
        if len(body) > budget:
            # Drop rather than emit a stub: a 200-char head of a file reads as a
            # complete file and invites findings about code the reviewer never saw.
            omitted += 1
            continue
        budget -= len(body)
        files.append(FileSlice(path=rel, text=body, truncated=truncated))

    return ReviewContext(
        diff=diff,
        files=tuple(files),
        omitted=omitted,
        diff_truncated=diff_truncated,
        skipped_test_files=tuple(test_files),
        degraded=degraded,
    )
