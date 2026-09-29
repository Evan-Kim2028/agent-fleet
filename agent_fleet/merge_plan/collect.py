"""Discover gate-approved PRs and confirm each approval is still current.

Approvals come from two places, merged and de-duplicated by
``(repo, pr_number)``:

* the **lane registry** — ``~/.agent-fleet/lanes/<operator>/<lane>.json``,
  whose ``status_line`` carries e.g. ``PREMERGE-APPROVED 0dc2391ab``;
* a **status directory** (``--status-dir``) of files with the same marker.

A status file the fleet actually writes is ``<lane>.status`` and names its PR
nowhere, so a file that carries no reference is resolved by lane: the lane's
push branch (``fb/<lane>``, or ``dq1d/<x>`` for a ``dq1d-<x>`` lane) is looked
up in the repository's open PRs, once per run.

A PR is only batchable when the gate approved the SHA the head points at
*now*, and when that approval is the gate's **last word**: a status file is
append-only, so a lane approved at 18:37 and escalated at 19:02 has not been
approved.  A moved head means the gate never reviewed the new commits, so the
PR is reported as a stale approval and excluded from batching rather than
silently shipped.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_fleet.fleet_paths import agent_fleet_home
from agent_fleet.merge_plan.profile import build_profile, load_manifest_parent_map
from agent_fleet.merge_plan.types import APPROVAL_PREFIX, ApprovedPR, ChangeProfile, Verdict

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from agent_fleet.merge_plan.types import RepoSpec

logger = logging.getLogger(__name__)

#: Everything ``gh pr view`` is asked for, in one place.  ``gh`` emits exactly
#: the requested keys, so this list is the contract between GitHub and both
#: readers of the payload: the profiler needs the shape of the change, and the
#: executor needs to know whether the PR is open, mergeable, and what it
#: merged to.
PR_DETAIL_FIELDS = ",".join(
    (
        "state",
        "mergeable",
        "headRefOid",
        "headRefName",
        "baseRefName",
        "additions",
        "deletions",
        "files",
        "mergeCommit",
    )
)

#: The approval marker must be the *verdict* of the line that carries it, so the
#: search is anchored to the start of a line rather than run over the line's whole
#: text. An unanchored search is forgeable by anything else that gets written into
#: the line: the lane manager appends the tail of the implementer's own final
#: message to a ``NEEDS-ESCALATION`` line, so a lane that was never gated could
#: otherwise quote the PR's real head and hand the planner an approval for a PR
#: no gate ever reviewed. ``fleet_ops.gate`` anchors its equivalent for the same
#: reason.
#:
#: The anchor still admits the three forms the status-file contract actually
#: writes — a bare ``PREMERGE-APPROVED <sha>``, one behind the ``HH:MM:SS`` stamp,
#: and one behind the ``owner/repo#<pr>`` reference that names the PR (which
#: ``docs/MERGE-PLAN.md`` documents as legal "anywhere on the line"), so a status
#: file naming its PR before the marker keeps being collected.
_APPROVAL_RE = re.compile(
    rf"^(?:\d{{2}}:\d{{2}}:\d{{2}}\s+)?"
    rf"(?:[\w.-]+/[\w.-]+#\d+\s+)?"
    rf"{APPROVAL_PREFIX}\s+([0-9a-f]{{7,40}})",
    re.IGNORECASE | re.MULTILINE,
)
#: ``"repo#123"`` in a status file, so a status dir can span repositories.
_PR_REF_RE = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)#(?P<pr>\d+)")

#: A status file is a stamped event log, so a line is a verdict or it is
#: commentary.  Splitting the stamp off first is what lets a *reason* quote a
#: marker — ``NEEDS-ESCALATION reviewer said "needs PREMERGE-APPROVED"`` must
#: stay an escalation — and what tells the reference lines apart from verdicts.
_STATUS_LINE_RE = re.compile(r"^(?:\d{2}:\d{2}:\d{2}\s+)?(\S+)")
_VERDICT_TOKEN_RE = re.compile(r"^(?:[\w.-]+/[\w.-]+#\d+\s+)?([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)")

#: The gate writes one token per verdict (``fleet_ops.gate``), so a token the
#: gate has never written is the status file's own note — the lane manager
#: annotating a verdict, or an implementer echoing a marker as prose.  Neither
#: is a new decision by the gate, so neither may outvote the verdict it follows.
_GATE_VERDICT_TOKENS = frozenset({"NEEDS-ESCALATION", "NEEDS-REBASE", "GATE-SKIPPED", "MERGED"})


# ---------------------------------------------------------------------------
# Approval parsing
# ---------------------------------------------------------------------------


def parse_approval(text: str) -> str:
    """Return the approved short SHA *if the file's last verdict is an approval*.

    Only a line whose own verdict is the approval marker counts, and only when
    it is the verdict the gate left last.  A marker that merely appears somewhere
    in the text is not an approval: the same text can carry an escalation reason
    and a transcript of what the implementer said, and neither of those is the
    gate speaking.

    A status file is append-only history, so it holds every verdict the lane has
    ever drawn, and the first approval in the file is not the current one.  The
    gate revises: a lane cleared at 18:37 can be escalated at 19:02 because a
    rebase landed wrong.  Reading the *first* match therefore hands the planner
    an approval for a lane the gate has since refused, and merges it.  The last
    verdict wins for the same reason ``fleet_ops.gate`` classifies a transcript:
    only the gate's most recent word on the lane is a statement about it now.
    """
    return _last_verdict(text or "").approved_sha


def _last_verdict(text: str) -> Verdict:
    """The gate's most recent verdict in *text*, judged line by line.

    Only lines that *are* a verdict are considered, so a reason that merely
    mentions a marker cannot outvote the verdict that follows it.  Scanning
    forward and letting each verdict overwrite the last one also puts a stray
    approval quoted in an implementer's own log tail after the verdict that
    refused the lane, which is the direction that is safe to fail.
    """
    verdict = Verdict()
    for line in (text or "").splitlines():
        approval = _APPROVAL_RE.match(line)
        if approval:
            verdict = Verdict(approved_sha=approval.group(1), line=line)
            continue
        marker = _verdict_token(line)
        if marker is not None and marker != APPROVAL_PREFIX and marker in _GATE_VERDICT_TOKENS:
            verdict = Verdict(verdict=marker, line=line)
    return verdict


def _verdict_token(line: str) -> str | None:
    """The gate marker *line* declares as its own verdict, or None.

    Anchored to the start of the line for the reason ``_APPROVAL_RE`` is: a
    lane that was never approved can still quote a marker inside its
    implementer's message, and that quote is not the gate speaking.  A line
    with no leading ``HH:MM:SS`` stamp and no marker is not a verdict at all
    — those are the ``owner/repo#12`` reference lines, which name a PR without
    saying anything about whether it is allowed to merge.
    """
    stamp = _STATUS_LINE_RE.match(line)
    if stamp is None:
        return None
    token = _VERDICT_TOKEN_RE.match(stamp.group(1))
    return token.group(1) if token else None


def lanes_dir() -> Path:
    """The lane registry root (``~/.agent-fleet/lanes``)."""
    return agent_fleet_home() / "lanes"


def _bare_repo(repo: str) -> str:
    """*repo*'s name without its owner — the spelling a checkout carries."""
    return repo.rsplit("/", 1)[-1]


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
    directory can hold several repositories.  A file that does not — which is
    what the real fleet writes, ``lanes/<lane>.status`` holding bare verdict
    lines — cannot name its own PR, so it is left for the caller to resolve by
    lane (see :func:`branch_names_for_lane`) rather than guessed at here.
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
            repo, pr_number = default_repo, 0
        else:
            continue
        found.append(
            ApprovedPR(
                repo=repo,
                pr_number=pr_number,
                approved_sha=sha,
                lane=path.stem,
                source="status_dir",
            )
        )
    return found


#: A lane's push branch is ``fb/<lane>`` (``fleet_ops.config.DEFAULT_PUSH_BRANCH``),
#: which is what makes a status file's own name enough to find its PR.
LANE_BRANCH_PREFIX = "fb"

#: A lane named ``dq1d-<x>`` runs on ``dq1d/<x>``: the operator replaced the
#: separator, and it applies to the rest of the name, not the whole stem.  So
#: ``dq1d-mergers`` is also tried as ``fb/dq1d-mergers``.
DQ1D_LANE_PREFIX = "dq1d"


def branch_names_for_lane(stem: str) -> tuple[str, ...]:
    """The push-branch names a lane named *stem* could have, most likely first.

    A lane is identified by its push branch rather than by a PR number written
    into its status file: the file is an append-only verdict log and the gate
    writes verdicts, not PR references, so the lane's branch is the only name
    the two files can be joined on.
    """
    candidates = [f"{LANE_BRANCH_PREFIX}/{stem}"]
    if stem.startswith(f"{DQ1D_LANE_PREFIX}-"):
        candidates.append(f"{DQ1D_LANE_PREFIX}/{stem[len(DQ1D_LANE_PREFIX) + 1 :]}")
    return tuple(candidates)


# ---------------------------------------------------------------------------
# Resolving a lane to its PR
# ---------------------------------------------------------------------------


class OpenPRIndex:
    """This run's open PRs by head branch, from a single ``gh pr list``.

    One call per run, not one per status file: a train over a dozen lanes that
    shelled out per file would pay a network round trip each time to learn the
    same list, and a gate that takes minutes can outlast a slow CLI.
    """

    def __init__(self, prs: Iterable[Mapping[str, Any]]) -> None:
        self._by_head: dict[str, list[int]] = {}
        for pr in prs:
            number = pr.get("number")
            head = str(pr.get("headRefName") or "")
            if not head or not isinstance(number, int) or number <= 0:
                continue
            self._by_head.setdefault(head, []).append(number)

    @classmethod
    def load(cls, client: GitHubClient) -> OpenPRIndex:
        """One ``gh pr list`` over the open PRs, keyed by head branch."""
        return cls(client.list_open_approved())

    def pr_numbers_for(self, lane: str) -> tuple[int, ...]:
        """Open PR numbers whose head is a branch *lane* could be pushed to."""
        found: list[int] = []
        for branch in branch_names_for_lane(lane):
            found.extend(n for n in self._by_head.get(branch, ()) if n not in found)
        return tuple(found)


def resolve_lane_approvals(
    approvals: Iterable[ApprovedPR],
    *,
    client: GitHubClient,
    repo: str,
) -> list[ApprovedPR]:
    """Fill in the PR number of approvals that name only a lane.

    A status file from the real fleet is ``lanes/<lane>.status`` and names its
    PR nowhere, so the approval arrives with no number and the train has nothing
    to batch.  The lane's push branch is the join: one listing of the open PRs
    turns ``fb/<lane>`` back into the PR it belongs to.

    An approval that already carries a number is passed through untouched, and a
    lane no open PR claims is dropped with a log — the planner can only ship
    open PRs, so keeping a numberless entry would just put an unreadable PR in
    front of the gate.  Where a branch is ambiguous (two open PRs off one
    branch) the smallest number wins, because an approval must resolve to a
    single PR or to none at all.
    """
    pending = [a for a in approvals if a.pr_number <= 0]
    resolved = [a for a in approvals if a.pr_number > 0]
    if not pending:
        return list(resolved)
    index = OpenPRIndex.load(client)
    for approval in pending:
        numbers = index.pr_numbers_for(approval.lane)
        if not numbers:
            logger.debug("no open PR on a branch for lane %r, skipping", approval.lane)
            continue
        resolved.append(
            replace(
                approval,
                pr_number=min(numbers),
                repo=approval.repo or repo,
            )
        )
    return resolved


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
        """``gh pr view <n> --json <the fields the planner and executor read>``.

        ``gh`` returns *only* the fields that were asked for, so a key missing
        from this list is missing from the payload no matter how true it is on
        GitHub.  The executor branches on ``state`` and ``mergeable`` and hands
        ``mergeCommit`` to the deploy command; asking for anything less makes
        every PR look unreadable and the queue silently never ships.
        ``headRefName`` and ``baseRefName`` together are what makes a stack
        legible: a PR whose base names another PR's head branch lands after it.

        Returns {} on any failure so a PR whose state cannot be read is
        treated as unverifiable rather than assumed mergeable.
        """
        result = self._run(
            "pr",
            "view",
            str(pr_number),
            "--json",
            PR_DETAIL_FIELDS,
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


def _scoped_client[ScopedT](client: ScopedT, repo_path: Path | None) -> ScopedT:
    """*client* bound to *repo_path*, so gh resolves the PR in that repository.

    A client that cannot re-scope itself (an injected test double) is used as
    given, which keeps the collector usable without a per-repo checkout.  The
    return type follows the input, so both the concrete client and a structural
    stand-in survive the call.
    """
    if repo_path is None:
        return client
    for_repo = getattr(client, "for_repo", None)
    if callable(for_repo):
        scoped = for_repo(repo_path)
        if isinstance(scoped, type(client)):
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

    The repo key is the bare name, so the two spellings one PR arrives under
    reconcile here rather than downstream: sources record ``owner/name`` while a
    ``--repo-path`` names the bare ``name``, and keying on the raw string lets
    one PR through twice.  Every caller only ever selects a repo afterwards, and
    a driver selects exactly one — so a bare key merges a spelling with the
    repository of the same name that was never the operator's target, while a
    driver pointed at the *other* repository still gets an empty selection
    rather than someone else's approvals.
    """
    by_key: dict[tuple[str, int], ApprovedPR] = {}
    for pr in sorted(entries, key=lambda p: (p.repo, p.pr_number, p.source)):
        by_key.setdefault((_bare_repo(pr.repo), pr.pr_number), pr)
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
