"""Discover gate-approved PRs and confirm each approval is still current.

Approvals come from two places, merged and de-duplicated by
``(repo, pr_number)``:

* the **lane registry** — ``~/.agent-fleet/lanes/<operator>/<lane>.json``,
  whose ``status_line`` carries e.g. ``PREMERGE-APPROVED 0dc2391ab``;
* a **status directory** (``--status-dir``) of files with the same marker.

A PR is only batchable when the gate approved the SHA the head points at
*now*.  A moved head means the gate never reviewed the new commits, so the PR
is reported as a stale approval and excluded from batching rather than
silently shipped.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_paths import agent_fleet_home
from agent_fleet.merge_plan.profile import build_profile, load_manifest_parent_map
from agent_fleet.merge_plan.types import APPROVAL_PREFIX, ApprovedPR, ChangeProfile

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agent_fleet.merge_plan.types import RepoSpec

logger = logging.getLogger(__name__)

_APPROVAL_RE = re.compile(rf"{APPROVAL_PREFIX}\s+([0-9a-f]{{7,40}})", re.IGNORECASE)
#: ``"repo#123"`` in a status file, so a status dir can span repositories.
_PR_REF_RE = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)#(?P<pr>\d+)")


# ---------------------------------------------------------------------------
# Approval parsing
# ---------------------------------------------------------------------------


def parse_approval(text: str) -> str:
    """Return the approved short SHA in *text*, or "" when absent."""
    m = _APPROVAL_RE.search(text or "")
    return m.group(1) if m else ""


def lanes_dir() -> Path:
    """The lane registry root (``~/.agent-fleet/lanes``)."""
    return agent_fleet_home() / "lanes"


def collect_from_lanes(
    *,
    operator: str | None = None,
    lanes_root: Path | None = None,
) -> list[ApprovedPR]:
    """Read approved PRs out of the lane registry.

    Only lanes carrying a non-null ``repo`` and ``pr`` can name a PR; the
    registry's schema is owned by the parallel ``fb/fleetops`` lane, so every
    field is treated as optional and a malformed file is skipped with a log
    rather than raising.
    """
    root = lanes_root or lanes_dir()
    if not root.is_dir():
        return []
    found: list[ApprovedPR] = []
    for lane_file in sorted(root.glob("*/*.json")):
        if operator and lane_file.parent.name != operator:
            continue
        try:
            data = json.loads(lane_file.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            logger.debug("unreadable lane file, skipping: %s", lane_file)
            continue
        if not isinstance(data, dict):
            continue
        sha = parse_approval(str(data.get("status_line") or ""))
        repo = str(data.get("repo") or "")
        pr_raw = data.get("pr")
        if not sha or not repo or pr_raw in (None, ""):
            continue
        try:
            pr_number = int(pr_raw)
        except TypeError, ValueError:
            continue
        found.append(
            ApprovedPR(
                repo=repo,
                pr_number=pr_number,
                approved_sha=sha,
                operator=str(data.get("operator") or lane_file.parent.name),
                lane=str(data.get("lane") or lane_file.stem),
                source="lane",
            )
        )
    return found


def collect_from_status_dir(
    status_dir: Path,
    *,
    default_repo: str = "",
) -> list[ApprovedPR]:
    """Read approved PRs out of a directory of status files.

    Each file may carry ``owner/repo#123`` anywhere in its text so one
    directory can hold several repositories; without a reference the file's
    own name is used as the repo and the file must contain a bare
    ``<repo>#<pr>`` marker.
    """
    if not status_dir.is_dir():
        return []
    found: list[ApprovedPR] = []
    for path in sorted(status_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sha = parse_approval(text)
        if not sha:
            continue
        m = _PR_REF_RE.search(text)
        if m:
            repo, pr_number = m.group("repo"), int(m.group("pr"))
        elif default_repo:
            repo, pr_number = default_repo, _pr_number_from_name(path.stem)
        else:
            continue
        if not pr_number:
            continue
        found.append(
            ApprovedPR(
                repo=repo,
                pr_number=pr_number,
                approved_sha=sha,
                source="status_dir",
            )
        )
    return found


def _pr_number_from_name(stem: str) -> int:
    m = re.search(r"(\d+)", stem)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# gh access (thin, injectable)
# ---------------------------------------------------------------------------


class GitHubClient:
    """Minimal ``gh`` wrapper for the two fields merge-plan needs."""

    def __init__(self, *, cwd: Path | None = None, binary: str = "gh") -> None:
        self.cwd = cwd
        self.binary = binary

    def for_repo(self, repo_path: Path | None) -> GitHubClient:
        """A client scoped to *repo_path*, which is how ``gh`` finds a repo.

        ``gh pr view <n>`` resolves the PR number against the origin remote of
        the checkout it runs in, so a client bound to one checkout must not be
        reused for another repository.  Returns ``self`` when *repo_path* is
        empty, keeping the caller's cwd.
        """
        if repo_path is None:
            return self
        return GitHubClient(cwd=repo_path, binary=self.binary)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args],
            capture_output=True,
            text=True,
            cwd=str(self.cwd) if self.cwd else None,
            check=False,
            timeout=120,
        )

    def pr_detail(self, pr_number: int) -> dict[str, Any]:
        """``gh pr view <n> --json headRefOid,baseRefName,additions,deletions``.

        Returns {} on any failure so a PR whose head cannot be read is treated
        as unverifiable rather than assumed mergeable.
        """
        result = self._run(
            "pr",
            "view",
            str(pr_number),
            "--json",
            "headRefOid,baseRefName,additions,deletions,files",
        )
        if result.returncode != 0:
            logger.debug("gh pr view %s failed: %s", pr_number, result.stderr.strip())
            return {}
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError, TypeError:
            return {}
        return data if isinstance(data, dict) else {}

    def list_open_approved(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Open PRs with their head SHA, for sanity checks and reporting."""
        result = self._run(
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,headRefOid,headRefName",
            "--limit",
            str(limit),
        )
        if result.returncode != 0:
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError, TypeError:
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _scoped_client(client: GitHubClient, repo_path: Path | None) -> GitHubClient:
    """*client* bound to *repo_path*, so gh resolves the PR in that repository.

    A client that cannot re-scope itself (an injected test double) is used as
    given, which keeps the collector usable without a per-repo checkout.
    """
    if repo_path is None:
        return client
    for_repo = getattr(client, "for_repo", None)
    if callable(for_repo):
        scoped = for_repo(repo_path)
        if isinstance(scoped, GitHubClient):
            return scoped
    return client


def _files_from_detail(detail: Mapping[str, Any]) -> tuple[str, ...]:
    files = detail.get("files")
    if not isinstance(files, list):
        return ()
    return tuple(str(f.get("path", "")) for f in files if isinstance(f, dict) and f.get("path"))


# ---------------------------------------------------------------------------
# Collection pipeline
# ---------------------------------------------------------------------------


def dedupe_approvals(entries: Sequence[ApprovedPR]) -> list[ApprovedPR]:
    """Collapse duplicates by ``(repo, pr_number)``, ordered deterministically.

    A PR approved in both the lane registry and a status dir keeps the first
    entry after a stable sort, so the source recorded is reproducible.
    """
    by_key: dict[tuple[str, int], ApprovedPR] = {}
    for pr in sorted(entries, key=lambda p: (p.repo, p.pr_number, p.source)):
        by_key.setdefault((pr.repo, pr.pr_number), pr)
    return [by_key[k] for k in sorted(by_key)]


def profile_approvals(
    approvals: Sequence[ApprovedPR],
    *,
    client: GitHubClient,
    repo_specs: Mapping[str, RepoSpec],
) -> tuple[
    list[ApprovedPR],
    dict[tuple[str, int], ChangeProfile],
    list[ChangeProfile],
    list[ChangeProfile],
]:
    """Split approvals into batchable PRs, stale ones, and unprofilable ones.

    Returns ``(batchable, profiles, stale, unprofilable)``.  ``profiles`` is
    keyed by ``(repo, pr_number)``.  ``unprofilable`` holds PRs whose head
    could not be read at all — a transient ``gh`` failure, which is reported
    rather than treated as approval.
    """
    batchable: list[ApprovedPR] = []
    profiles: dict[tuple[str, int], ChangeProfile] = {}
    stale: list[ChangeProfile] = []
    unprofilable: list[ChangeProfile] = []
    parent_maps: dict[str, Mapping[str, list[str]]] = {}

    for pr in approvals:
        spec = repo_specs.get(pr.repo)
        repo_path = Path(spec.path).expanduser() if spec and spec.path else None
        # gh resolves the PR number against the checkout it runs in, so each
        # repo must be read through its own client.  One client pinned to the
        # first repo silently profiles every other repo's PRs against it.
        detail = _scoped_client(client, repo_path).pr_detail(pr.pr_number)
        head_sha = str(detail.get("headRefOid") or "")
        if not head_sha:
            unprofilable.append(
                ChangeProfile(
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    reason="could not read PR head (gh unavailable?)",
                )
            )
            continue
        current = ApprovedPR(
            repo=pr.repo,
            pr_number=pr.pr_number,
            approved_sha=pr.approved_sha,
            head_sha=head_sha,
            operator=pr.operator,
            lane=pr.lane,
            source=pr.source,
        )
        if current.is_stale:
            stale.append(
                ChangeProfile(
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    stale=True,
                    reason=(f"stale approval: gate approved {pr.sha9}, head is now {head_sha[:9]}"),
                )
            )
            continue

        parent_map = _parent_map_for(repo_path, spec, parent_maps)
        batchable.append(current)
        profiles[(pr.repo, pr.pr_number)] = build_profile(
            current,
            files=_files_from_detail(detail),
            base_ref=str(detail.get("baseRefName") or ""),
            lines_changed=int(detail.get("additions") or 0) + int(detail.get("deletions") or 0),
            repo_spec=spec,
            parent_map=parent_map,
        )

    batchable.sort(key=lambda p: (p.repo, p.pr_number))
    return batchable, profiles, stale, unprofilable


def _parent_map_for(
    repo_path: Path | None,
    spec: RepoSpec | None,
    cache: dict[str, Mapping[str, list[str]]],
) -> Mapping[str, list[str]] | None:
    """Load (and cache) the dbt manifest parent_map for a repo, if present."""
    if repo_path is None:
        return None
    key = str(repo_path)
    if key not in cache:
        rel = spec.dbt_manifest_path if spec else "transform/target/manifest.json"
        loaded = load_manifest_parent_map(repo_path / rel)
        cache[key] = loaded if loaded else {}
    return cache[key] or None
