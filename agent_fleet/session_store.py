"""Cross-process persistence for resumable backend session ids.

Keyed by worktree path (not run_id): ``ResumableGitOps.attach_worktree``
reattaches the *same* worktree path when a fleet run resumes an interrupted
task (see ``agent_fleet/integrations/local_git.py``), so keying the sidecar
file by worktree path lets resume find the right durable-session id (e.g. a
Devin CLI ``session_id`` for ``-r``) without writing anything into the
worktree's own git-visible file tree — a fleet worktree must stay clean of
fleet artifacts for ``git status`` (concurrent dispatch checks this).

Every function here is best-effort and never raises: session-id continuity is
an optimization (skip re-explaining the task from scratch on resume), not a
correctness requirement, so a storage hiccup must never break a run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_STORE_DIR = Path("~/.agent-fleet/session_store").expanduser()


def _key_path(worktree: str, *, store_dir: Path | None = None) -> Path:
    digest = hashlib.sha1(worktree.encode("utf-8")).hexdigest()
    return (store_dir or _STORE_DIR) / f"{digest}.json"


def persist_session_id(worktree: str, session_id: str, *, store_dir: Path | None = None) -> None:
    """Best-effort write of *session_id* for *worktree*. Never raises."""
    if not worktree or not session_id:
        return
    try:
        target_dir = store_dir or _STORE_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        _key_path(worktree, store_dir=store_dir).write_text(
            json.dumps({"worktree": worktree, "session_id": session_id}),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("session_store: persist failed for worktree=%s", worktree, exc_info=True)


def load_session_id(worktree: str, *, store_dir: Path | None = None) -> str | None:
    """Best-effort read of a previously persisted session id for *worktree*."""
    if not worktree:
        return None
    path = _key_path(worktree, store_dir=store_dir)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def clear_session_id(worktree: str, *, store_dir: Path | None = None) -> None:
    """Best-effort removal, e.g. once a worktree is torn down for good."""
    if not worktree:
        return
    try:
        _key_path(worktree, store_dir=store_dir).unlink(missing_ok=True)
    except OSError:
        logger.debug("session_store: clear failed for worktree=%s", worktree, exc_info=True)
