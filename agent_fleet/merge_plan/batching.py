"""The merge-batching algorithm.

The goal is to spend the fewest deploys.  A lake-of-rage deploy is ~15-20
minutes and a silphcoanalytics deploy ~9, against a merged PR that takes
seconds, so the plan exists to make one deploy cover as many approved PRs as
it safely can.

Rules, in the order they are applied
------------------------------------
1. **One deploy unit per batch.**  A batch ships behind a single deploy and a
   single verify, so every PR in it must share a deploy unit.  A PR touching
   two units takes the heavier one (see ``profile._UNIT_PRECEDENCE``).
2. **No file overlap.**  Two PRs touching the same file never share a batch.
   PRs are walked in ascending PR number, so on a conflict the lower number
   lands first — oldest work first, which is both a stable repo fact and the
   order an operator would expect.
3. **dbt models are rebuilt once.**  PRs that touch a shared dbt model are
   unioned into one group up front, so their downstream rebuild runs a single
   time instead of once per PR.
4. **Risky PRs are alone, and ship last.**  A migration, a deploy script, or a
   prod-write tool is never batched with anything else: when it breaks, the
   blast radius is exactly that one PR.  Those batches go last so every safe
   batch has already landed and verified, and a failing risky deploy rolls
   back with no unverified changes riding along.
5. **Size cap.**  Batches are capped (default 5).  The cap is a hard safety
   bound; dbt grouping is best-effort *within* it, so a dbt group larger than
   the cap splits into consecutive sub-batches, each with its own ``--select``.
   The split is labelled so the operator knows the rebuild runs more than once.

Determinism
-----------
Every ordering is by ``(repo, pr_number)`` — values that do not change between
two runs over the same input.  No clock, no filesystem iteration order, and no
set iteration reaches the output, so identical input yields byte-identical
plan JSON.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from agent_fleet.merge_plan.types import (
    DEFAULT_MAX_BATCH_SIZE,
    Batch,
    ChangeProfile,
    RepoSpec,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agent_fleet.merge_plan.types import ApprovedPR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# dbt grouping
# ---------------------------------------------------------------------------


def dbt_groups(profiles: Sequence[ChangeProfile]) -> list[list[ChangeProfile]]:
    """Union PRs that share a dbt model, via connected components.

    Two PRs land in the same group when their downstream ``--select`` sets
    intersect, so the rebuild that covers both runs once.  Uses union-find
    keyed by PR number; groups are returned in ascending first-PR order.
    """
    with_models = [p for p in profiles if p.dbt_select]
    if not with_models:
        return []
    owner: dict[str, ChangeProfile] = {}
    parent: dict[int, int] = {p.pr_number: p.pr_number for p in with_models}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for profile in with_models:
        for model in profile.dbt_select:
            if model in owner:
                union(owner[model].pr_number, profile.pr_number)
            else:
                owner[model] = profile

    grouped: dict[int, list[ChangeProfile]] = {}
    for profile in with_models:
        grouped.setdefault(find(profile.pr_number), []).append(profile)
    return [sorted(g, key=lambda p: p.pr_number) for _, g in sorted(grouped.items())]


# ---------------------------------------------------------------------------
# Merge compatibility
# ---------------------------------------------------------------------------


def _git_version(repo_path: Path) -> tuple[int, ...]:
    try:
        out = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            cwd=str(repo_path),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    for token in out.split():
        if token.replace(".", "").isdigit():
            return tuple(int(p) for p in token.split("."))
    return ()


def _commit_exists(repo_path: Path, sha: str) -> bool:
    """Whether *sha* is a commit the local object store already has."""
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            cwd=str(repo_path),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def ensure_commits_local(
    repo_path: Path,
    shas: Sequence[str],
    prs: Sequence[ApprovedPR] = (),
) -> None:
    """Fetch any of *shas* the checkout does not already have.

    ``gh pr view`` reports head commits that live on the remote; a working copy
    that has not fetched since the PR opened does not have those objects.
    Handing a missing object to the merge check makes it answer "incompatible",
    which demotes every batch to single PRs and silently defeats batching.

    Each missing commit is fetched twice over: by SHA, for a remote that
    advertises ``uploadpack.allowReachableSHA1InWant``, and as
    ``refs/pull/<n>/head``, which GitHub always serves.  A commit that cannot
    be materialised at all is left to the merge check, which then reports the
    batch as unmergeable rather than pretending it verified.
    """
    missing = [sha for sha in shas if sha and not _commit_exists(repo_path, sha)]
    if not missing:
        return
    prs_by_sha = {(p.head_sha or p.approved_sha): p for p in prs}
    for sha in missing:
        pr = prs_by_sha.get(sha)
        refspecs = [sha]
        if pr is not None and pr.pr_number:
            refspecs.insert(0, f"+refs/pull/{pr.pr_number}/head")
        if _fetch(repo_path, *refspecs):
            continue
        if refspecs and refspecs[0] != sha:
            _fetch(repo_path, sha)


def _fetch(repo_path: Path, *refspecs: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "fetch", "--quiet", "origin", *refspecs],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
            cwd=str(repo_path),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("mergecheck fetch %s failed: %s", refspecs, exc)
        return False
    if result.returncode != 0:
        logger.debug("mergecheck fetch %s failed: %s", refspecs, result.stderr.strip())
    return result.returncode == 0


def _merge_tree_compatible(
    repo_path: Path,
    shas: Sequence[str],
) -> bool | None:
    """Try ``git merge-tree --write-tree`` (git >= 2.38).

    Returns True/False on a verdict, or None when this git is too old for the
    command, so the caller falls back to a scratch worktree.

    Each PR is folded onto the base branch in turn and the resulting tree is
    turned back into a commit, so the next ``merge-tree`` gets a *commit* as
    its base.  ``--write-tree`` prints a tree oid, and feeding that back as
    the next base makes git >= 2.38 reject it ("expected commit type"), so a
    correct 3+ PR fold needs the ``commit-tree`` round trip.
    """
    if _git_version(repo_path) < (2, 38):
        return None
    base = shas[0]
    for sha in shas[1:]:
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", base, sha],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=str(repo_path),
        )
        if result.returncode != 0:
            return False
        parts = result.stdout.split()
        if not parts:
            return False
        # A clean merge prints just the tree oid; a conflicted one exits nonzero
        # above, so whatever reaches here is a tree that must be committed back
        # into a commit before the next fold.
        committed = _commit_tree(repo_path, parts[0], base, sha)
        if committed is None:
            return False
        base = committed
    return True


def _commit_tree(repo_path: Path, tree: str, base: str, other: str) -> str | None:
    """Record *tree* as a commit so it can be used as the next merge base."""
    result = subprocess.run(
        ["git", "commit-tree", tree, "-p", base, "-p", other, "-m", "merge-plan fold"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=str(repo_path),
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _scratch_merge_compatible(
    repo_path: Path,
    shas: Sequence[str],
) -> bool:
    """Fallback: replay the merges in a throwaway worktree.

    Each SHA is merged with ``--no-commit`` and the result committed as a
    throwaway merge commit, because ``git merge`` refuses to start a second
    merge while MERGE_HEAD from the first is still outstanding — without the
    commit, the third and later PR of a batch cannot merge at all and the whole
    batch is falsely reported as incompatible.  A refused merge (nonzero exit)
    is a real conflict and returns False; the exit status is read from the
    process itself because git reports "Automatic merge went well" on stdout
    while still exiting 1.
    The worktree is removed in a ``finally`` — it is created by this function, so
    removing it is safe, but the repo itself is never mutated.
    """
    if not shas:
        return True
    tmp = Path(tempfile.mkdtemp(prefix="fleet-mergecheck-"))
    worktree = tmp / "wt"
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), shas[0]],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            cwd=str(repo_path),
        )
        if add.returncode != 0:
            logger.debug("mergecheck worktree add failed: %s", add.stderr.strip())
            return False
        for sha in shas[1:]:
            merge = subprocess.run(
                ["git", "merge", "--no-commit", "--no-ff", sha],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
                cwd=str(worktree),
            )
            if merge.returncode != 0:
                return False
            commit = subprocess.run(
                ["git", "commit", "--no-gpg-sign", "-q", "-m", "merge-plan check"],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
                cwd=str(worktree),
            )
            if commit.returncode != 0:
                logger.debug("mergecheck commit failed: %s", commit.stderr.strip())
                return False
        return True
    finally:
        # Detach and remove only the worktree this call created.
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=str(repo_path),
        )
        shutil.rmtree(tmp, ignore_errors=True)


def merge_compatible(
    *,
    repo_path: Path | None,
    shas: Sequence[str],
    check: bool = True,
    prs: Sequence[ApprovedPR] = (),
) -> bool:
    """Whether *shas* merge cleanly together, in the order given.

    With ``check=False`` (or no repo path) the call is a no-op returning True,
    which is what the planner uses when file-overlap analysis has already
    proven the batch disjoint.
    """
    if not check or not shas or repo_path is None:
        return True
    if not repo_path.is_dir():
        return True
    # The head SHAs come from the GitHub API, not from this checkout, so they
    # are fetched before the check rather than being reported as unmergeable.
    ensure_commits_local(repo_path, shas, prs)
    if not all(_commit_exists(repo_path, sha) for sha in shas if sha):
        logger.debug("mergecheck: PR head commits unavailable in %s", repo_path)
        return False
    verdict = _merge_tree_compatible(repo_path, shas)
    if verdict is not None:
        return verdict
    return _scratch_merge_compatible(repo_path, shas)


# ---------------------------------------------------------------------------
# Executor command rendering
# ---------------------------------------------------------------------------


def render_executor_commands(batch_prs: Sequence[ApprovedPR], spec: RepoSpec | None) -> list[str]:
    """Render the operator's merge command lines for one batch.

    Two shapes are supported, matching how the two operator sessions merge:
    a single ``merge_template`` taking many ``{pr}:{sha9}`` arguments, or a
    ``merge_per_pr_template`` repeated once per PR and chained.  With no
    template configured the result is empty and the CLI says so, rather than
    inventing a command that may not exist.
    """
    if spec is None:
        return []
    if spec.merge_template:
        pr_args = " ".join(f"{p.pr_number}:{p.sha9}" for p in batch_prs)
        return [spec.merge_template.replace("{pr_args}", pr_args)]
    if spec.merge_per_pr_template:
        return [
            spec.merge_per_pr_template.replace("{pr}", str(p.pr_number)).replace("{sha9}", p.sha9)
            for p in batch_prs
        ]
    return []


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


def _files_overlap(a: ChangeProfile, b: ChangeProfile) -> bool:
    return bool(set(a.files) & set(b.files))


def _batches_within_group(
    group: Sequence[ChangeProfile],
    *,
    max_batch_size: int,
) -> list[list[ChangeProfile]]:
    """Pack a dbt group into batches honouring the cap, preserving PR order.

    Split only ever happens because of the cap; the caller marks the batch so
    the operator knows the rebuild runs once per sub-batch.
    """
    chunks: list[list[ChangeProfile]] = []
    current: list[ChangeProfile] = []
    for profile in group:
        if current and len(current) >= max_batch_size:
            chunks.append(current)
            current = []
        current.append(profile)
    if current:
        chunks.append(current)
    return chunks


def _pack_group(
    group: Sequence[ChangeProfile],
    *,
    max_batch_size: int,
) -> list[list[ChangeProfile]]:
    """Pack PRs sharing dbt models into batches, splitting on file overlap.

    Walks in PR order and starts a new batch whenever the next PR would share
    a file with the batch being built, or the cap is reached.
    """
    batches: list[list[ChangeProfile]] = []
    current: list[ChangeProfile] = []
    used_files: set[str] = set()
    for profile in group:
        if current and (len(current) >= max_batch_size or set(profile.files) & used_files):
            batches.append(current)
            current = []
            used_files = set()
        current.append(profile)
        used_files |= set(profile.files)
    if current:
        batches.append(current)
    return batches


def _reasons_for(
    members: Sequence[ChangeProfile],
    *,
    split: bool,
    total_in_group: int,
    unit: str,
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
) -> list[str]:
    reasons = [
        f"{len(members)} PR(s) share deploy unit '{unit}' — one deploy + verify covers the batch"
    ]
    dbt = sorted({m for p in members for m in p.dbt_select})
    if dbt:
        shown = ", ".join(dbt[:4]) + (f", +{len(dbt) - 4} more" if len(dbt) > 4 else "")
        reasons.append(f"dbt --select rebuild runs once for: {shown}")
        if split and total_in_group > len(members):
            reasons.append(
                f"dbt group of {total_in_group} PRs split by the {max_batch_size}-PR cap; "
                "the rebuild runs once per sub-batch"
            )
    if any(p.is_risky for p in members):
        flags = sorted({f for p in members for f in p.risk_flags})
        reasons.append(f"isolated for risk: {', '.join(flags)}")
    return reasons


def plan_batches(
    prs: Sequence[ApprovedPR],
    profiles: Mapping[tuple[str, int], ChangeProfile],
    *,
    repo_specs: Mapping[str, RepoSpec],
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
    check_merges: bool = True,
) -> list[Batch]:
    """Build the ordered batches for *prs*.

    Ordering: all safe batches first, in ascending first-PR order, then the
    isolated risk batches.  Within a repo the order is stable; repos are
    processed in ascending name order.
    """
    by_repo: dict[str, list[ApprovedPR]] = {}
    for pr in sorted(prs, key=lambda p: (p.repo, p.pr_number)):
        by_repo.setdefault(pr.repo, []).append(pr)

    batches: list[Batch] = []
    for repo in sorted(by_repo):
        repo_prs = by_repo[repo]
        repo_profiles = [profiles[(p.repo, p.pr_number)] for p in repo_prs]
        spec = repo_specs.get(repo)
        repo_path = Path(spec.path).expanduser() if spec and spec.path else None

        # Rule 3: union PRs sharing dbt models, so their rebuild runs once.
        # A PR with no dbt models joins the "free" pool, which is packed by
        # deploy unit and file overlap so unrelated PRs still share a deploy.
        groups: list[list[ChangeProfile]] = []
        grouped_prs: set[int] = set()
        free: list[ChangeProfile] = []
        for group in dbt_groups(repo_profiles):
            grouped_prs.update(p.pr_number for p in group)
            groups.append(group)
        for profile in repo_profiles:
            if profile.pr_number in grouped_prs:
                continue
            if profile.is_risky:
                # Rule 4: a risky PR is never packed with anything.
                groups.append([profile])
            else:
                free.append(profile)
        # Split dbt groups on file overlap / cap.
        split_groups: list[list[ChangeProfile]] = []
        for group in groups:
            split_groups.extend(_pack_group(group, max_batch_size=max_batch_size))
        # Pack the free pool by deploy unit, then split on overlap / cap.
        free.sort(key=lambda p: (p.deploy_unit, p.pr_number))
        by_unit: dict[str, list[ChangeProfile]] = {}
        for profile in free:
            by_unit.setdefault(profile.deploy_unit, []).append(profile)
        for unit in sorted(by_unit):
            split_groups.extend(_pack_group(by_unit[unit], max_batch_size=max_batch_size))
        groups = split_groups

        safe: list[tuple[list[ChangeProfile], bool]] = []
        risky: list[tuple[list[ChangeProfile], bool]] = []
        # group_sizes[first_pr_number] is the size of the dbt group this chunk
        # came from, so a chunk smaller than its group is known to be a split.
        group_sizes: dict[int, int] = {}
        for group in groups:
            if len(group) > 1:
                group_sizes[min(p.pr_number for p in group)] = len(group)
        for chunk in groups:
            risky_here = any(p.is_risky for p in chunk)
            if risky_here:
                # Rule 4: a risky PR is alone in its batch.
                risky.extend(([p], True) for p in chunk)
            else:
                first_pr = min(p.pr_number for p in chunk)
                safe.append((chunk, group_sizes.get(first_pr, 1) > 1))

        # Rule 1: a batch ships behind one deploy, so it holds one deploy unit.
        safe = _split_mixed_units(safe)
        risky = _split_mixed_units(risky)

        # Rule 2 fallback: if the members still cannot merge cleanly together,
        # peel them back to single-PR batches rather than shipping a merge the
        # operator cannot land.
        if check_merges and repo_path is not None:
            safe, risky = _enforce_merge_compatibility(
                safe, risky, repo_prs=repo_prs, repo_path=repo_path
            )

        safe.sort(key=lambda pair: pair[0][0].pr_number)
        risky.sort(key=lambda pair: pair[0][0].pr_number)

        index = len(batches)
        for chunk, split in [*safe, *risky]:
            members = sorted(chunk, key=lambda p: p.pr_number)
            unit = next((p.deploy_unit for p in members if p.deploy_unit), "")
            wanted = {m.pr_number for m in members}
            batch_prs = tuple(p for p in repo_prs if p.pr_number in wanted)
            # Every PR sharing a --select model across the repo belongs to the
            # same dbt rebuild, so that is the honest "group size" to report.
            chunk_models = {m for p in members for m in p.dbt_select}
            total_in_group = (
                sum(1 for p in repo_profiles if set(p.dbt_select) & chunk_models)
                if chunk_models
                else len(members)
            )
            reasons = _reasons_for(
                members,
                split=split,
                total_in_group=total_in_group,
                unit=unit,
                max_batch_size=max_batch_size,
            )
            dbt_select = tuple(sorted({m for p in members for m in p.dbt_select}))
            isolated = any(p.is_risky for p in members)
            batches.append(
                Batch(
                    index=index,
                    repo=repo,
                    prs=batch_prs,
                    deploy_unit=unit,
                    reasons=tuple(reasons),
                    dbt_select=dbt_select,
                    executor_commands=tuple(render_executor_commands(batch_prs, spec)),
                    dbt_group_split=split and bool(dbt_select),
                    isolated_risk=isolated,
                )
            )
            index += 1

    return [
        Batch(
            index=i,
            repo=b.repo,
            prs=b.prs,
            deploy_unit=b.deploy_unit,
            reasons=b.reasons,
            dbt_select=b.dbt_select,
            executor_commands=b.executor_commands,
            dbt_group_split=b.dbt_group_split,
            isolated_risk=b.isolated_risk,
        )
        for i, b in enumerate(batches)
    ]


def _split_mixed_units(
    chunks: Sequence[tuple[list[ChangeProfile], bool]],
) -> list[tuple[list[ChangeProfile], bool]]:
    """Split any chunk whose PRs do not share one deploy unit."""
    out: list[tuple[list[ChangeProfile], bool]] = []
    for chunk, split in chunks:
        units = {p.deploy_unit for p in chunk}
        if len(units) <= 1:
            out.append((chunk, split))
        else:
            out.extend(([p], False) for p in sorted(chunk, key=lambda p: p.pr_number))
    return out


def _enforce_merge_compatibility(
    safe: list[tuple[list[ChangeProfile], bool]],
    risky: list[tuple[list[ChangeProfile], bool]],
    *,
    repo_prs: Sequence[ApprovedPR],
    repo_path: Path,
) -> tuple[list[tuple[list[ChangeProfile], bool]], list[tuple[list[ChangeProfile], bool]]]:
    """Demote any batch whose members do not merge cleanly to single-PR batches."""
    sha_by_pr = {p.pr_number: (p.head_sha or p.approved_sha) for p in repo_prs}
    out_safe: list[tuple[list[ChangeProfile], bool]] = []
    out_risky: list[tuple[list[ChangeProfile], bool]] = []
    for chunk, split in [*safe, *risky]:
        members = sorted(chunk, key=lambda p: p.pr_number)
        if len(members) == 1:
            (out_risky if members[0].is_risky else out_safe).append((members, split))
            continue
        shas = [sha_by_pr[m.pr_number] for m in members]
        if merge_compatible(repo_path=repo_path, shas=shas, check=True, prs=repo_prs):
            (out_risky if any(m.is_risky for m in members) else out_safe).append((members, split))
        else:
            for member in members:
                target = out_risky if member.is_risky else out_safe
                target.append(([member], False))
    return out_safe, out_risky
