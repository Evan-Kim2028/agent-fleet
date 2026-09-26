"""Memory-capped pytest execution for the gate's deterministic steps.

Two jobs:

**Cap the memory.** A 36GB pytest runaway is a real failure mode on this machine,
so every pytest the gate launches is wrapped in
``systemd-run --user --scope -p MemoryMax=6G -p MemorySwapMax=0`` when systemd
user scopes are available, falling back to ``ulimit -v`` in a subshell. The cap
is configurable but the gate never raises it above the configured value.

**Distinguish a test failure from an infra failure.** pytest exit codes are
load-bearing here: ``0`` all passed, ``1`` tests failed (a real finding), ``>=2``
interrupted / collection error / usage error, which means we learned nothing
about the code. The gate treats ``>=2`` as an infra error, never as a blocker.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Where cached pytest results live unless the gate config points elsewhere.
DEFAULT_CACHE_DIR = Path("~/.agent-fleet/cache/gate-tests")

#: A cached result older than this is re-run. The key already covers the whole
#: worktree, so the TTL is only a bound on disk growth and on how stale a
#: nondeterministic (flaky) result can be replayed.
DEFAULT_CACHE_TTL_S = 24 * 3600

#: Current on-disk format version, stored in every entry. A bump invalidates
#: every previously written entry instead of replaying it under new semantics.
_CACHE_VERSION = 1

# pytest exit codes we act on (see pytest docs, ExitCodes).
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1
PYTEST_INFRA_ERROR = 2

_MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)B?$", re.IGNORECASE)
_FACTOR = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}

_TEST_FILE_RE = re.compile(r"(^|/)test_[^/]*\.py$")

_FAILED_LINE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^SUMMARY:.*$", re.MULTILINE)


def memory_to_bytes(value: str) -> int:
    """Parse ``6G`` / ``512M`` / ``1073741824`` into bytes."""
    match = _MEMORY_RE.match(value.strip())
    if not match:
        raise ValueError(f"unparseable memory limit: {value!r}")
    number, unit = match.groups()
    return int(float(number) * _FACTOR[unit.upper()])


def systemd_run_available() -> bool:
    """True when ``systemd-run --user --scope`` can be used on this host."""
    if shutil.which("systemd-run") is None:
        return False
    try:
        probe = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "true"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return probe.returncode == 0


def is_test_file(path: str | Path) -> bool:
    """True for ``test_*.py`` paths anywhere in a repo (the gate's test selector)."""
    return bool(_TEST_FILE_RE.search(Path(path).as_posix()))


@dataclass(frozen=True)
class TestPackage:
    """A directory that owns a ``pyproject.toml`` and the tests beneath it.

    Multi-package repos (a root plus an ``api/`` sub-package) need pytest run
    from the package directory, not the repo root, so the venv and ``pythonpath``
    match. ``dir`` is the package root; ``tests`` are repo-relative paths.
    """

    dir: Path
    rel_dir: str
    tests: list[str] = field(default_factory=list)

    @property
    def local_tests(self) -> list[str]:
        """``tests`` relative to :attr:`dir` — pytest runs with ``cwd=dir``."""
        return [to_package_path(self.rel_dir, t) for t in self.tests]

    def command(self, extra: Sequence[str] = ()) -> list[str]:
        return [*uv_run_prefix(self.dir), "pytest", "-q", "--no-header", *extra, *self.local_tests]


def to_package_path(rel_dir: str, repo_path: str) -> str:
    """Repo-relative *repo_path* -> path relative to the package at *rel_dir*."""
    if rel_dir in ("", "."):
        return repo_path
    prefix = rel_dir.rstrip("/") + "/"
    return repo_path[len(prefix) :] if repo_path.startswith(prefix) else repo_path


def to_repo_node_id(rel_dir: str, node_id: str) -> str:
    """Package-relative pytest node id -> repo-relative (verify/fix/recheck use repo paths)."""
    if rel_dir in ("", "."):
        return node_id
    prefix = rel_dir.rstrip("/") + "/"
    return node_id if node_id.startswith(prefix) else prefix + node_id


def is_uv_workspace_root(package_dir: Path) -> bool:
    """True when *package_dir*'s pyproject declares a uv workspace.

    ``uv run`` at a workspace root syncs only the root project, so tests that
    import workspace members fail with ModuleNotFoundError in a fresh worktree.
    """
    try:
        text = (package_dir / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"^\[tool\.uv\.workspace\]", text, re.M) is not None


def uv_run_prefix(package_dir: Path | None) -> list[str]:
    """``uv run`` (+ ``--all-packages`` at a uv workspace root)."""
    if package_dir is not None and is_uv_workspace_root(package_dir):
        return ["uv", "run", "--all-packages"]
    return ["uv", "run"]


@dataclass(frozen=True)
class PytestResult:
    """Outcome of one pytest invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == PYTEST_OK

    @property
    def tests_failed(self) -> bool:
        return self.returncode == PYTEST_TESTS_FAILED

    @property
    def infra_error(self) -> bool:
        """True when the run told us nothing about the code under test."""
        return self.returncode >= PYTEST_INFRA_ERROR

    @property
    def failed_ids(self) -> list[str]:
        """Node ids of failing tests, parsed from pytest's ``FAILED`` lines."""
        return _FAILED_LINE_RE.findall(self.stdout)

    @property
    def summary(self) -> str:
        matches = _SUMMARY_RE.findall(self.stdout)
        if matches:
            return matches[-1][:300]
        tail = (self.stdout or "").strip().splitlines()[-1:] if self.stdout else []
        return tail[0][:300] if tail else ""


def find_test_packages(
    root: Path,
    test_files: Sequence[str],
    *,
    package_dir: str | None = None,
) -> list[TestPackage]:
    """Group repo-relative *test_files* by the nearest ancestor with pyproject.toml.

    A file belongs to the deepest directory at or above it that contains a
    ``pyproject.toml``; files with no such ancestor fall back to the repo root.
    ``package_dir`` forces everything into one package (the single-package case
    where the root ``pyproject.toml`` is not the one that owns the tests).
    """
    by_dir: dict[str, list[str]] = {}
    for rel in test_files:
        rel_path = Path(rel)
        owner = package_dir or _owning_package(root, rel_path)
        by_dir.setdefault(owner, []).append(rel_path.as_posix())
    return [
        TestPackage(dir=root / rel_dir, rel_dir=rel_dir, tests=sorted(tests))
        for rel_dir, tests in sorted(by_dir.items())
    ]


def _owning_package(root: Path, rel_path: Path) -> str:
    """Relative path of the nearest ancestor directory holding a pyproject.toml."""
    parts = rel_path.parent.parts
    for i in range(len(parts), -1, -1):
        candidate = root / Path(*parts[:i]) if i else root
        if (candidate / "pyproject.toml").is_file():
            return candidate.relative_to(root).as_posix() if i else "."
    return "."


def build_pytest_command(
    test_files: Sequence[str],
    *,
    memory: str = "6G",
    use_systemd: bool | None = None,
    package_dir: Path | None = None,
) -> list[str]:
    """Build the pytest command, wrapped in a memory cap when possible.

    *test_files* must be relative to *package_dir* (the cwd pytest runs in).
    """
    inner = [
        *uv_run_prefix(package_dir),
        "pytest",
        "-q",
        "--no-header",
        *test_files,
    ]
    if use_systemd is None:
        use_systemd = systemd_run_available()
    if not use_systemd:
        return inner
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "-p",
        f"MemoryMax={memory}",
        "-p",
        "MemorySwapMax=0",
        *inner,
    ]


def run_pytest(
    package_dir: Path,
    test_files: Sequence[str],
    *,
    memory: str = "6G",
    timeout_s: int = 900,
    use_systemd: bool | None = None,
    cache_dir: Path | None = None,
    cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
) -> PytestResult:
    """Run the given tests from *package_dir* under a memory cap.

    Never raises on a non-zero exit — the caller inspects
    :attr:`PytestResult.infra_error` to tell "the code is broken" apart from
    "pytest could not run".

    With *cache_dir* set, a run whose worktree tree hash and test list match a
    stored result is served from disk instead of re-running pytest. A worktree
    that is not a git repo (or a git that cannot write a tree) simply runs
    uncached rather than failing.
    """
    if not test_files:
        return PytestResult(returncode=PYTEST_OK, stdout="", stderr="")
    key = None
    cache_root = cache_dir.expanduser() if cache_dir is not None else None
    if cache_root is not None:
        prune_cache(cache_root, cache_ttl_s)
        tree = worktree_tree_hash(package_dir)
        if tree is not None:
            key = cache_key(tree, test_files, package=str(package_dir))
            cached = cache_load(cache_root, key, ttl_s=cache_ttl_s)
            if cached is not None:
                logger.info("gate pytest: cache hit %s (%d file(s))", key[:12], len(test_files))
                return cached
    cmd = build_pytest_command(
        test_files, memory=memory, use_systemd=use_systemd, package_dir=package_dir
    )
    logger.debug("gate pytest: %s (cwd=%s)", " ".join(cmd), package_dir)
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=package_dir,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # A timeout is an infra failure, not evidence the code is broken.
        return PytestResult(
            returncode=PYTEST_INFRA_ERROR,
            stdout="",
            stderr=f"pytest timed out after {timeout_s}s",
        )
    except OSError as exc:
        return PytestResult(returncode=PYTEST_INFRA_ERROR, stdout="", stderr=str(exc))
    result = PytestResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if key is not None and cache_root is not None:
        cache_store(cache_root, key, result)
    return result


# ---------------------------------------------------------------------------
# Result cache
# ---------------------------------------------------------------------------


def worktree_tree_hash(root: Path) -> str | None:
    """Git tree hash of the WHOLE worktree, or ``None`` if it cannot be computed.

    ``git write-tree`` on a *temporary* index: the real index is copied first,
    then ``git add -A`` is run against the copy, so the hash covers committed
    state plus every uncommitted edit, deletion and untracked file. Keying on
    the HEAD sha instead would let a gate replay a result for code the fixer has
    already changed, which is exactly the stale evidence the cache must not
    serve.

    The real index is never touched: the gate runs agents against this worktree
    and a clobbered index would be visible to the next caller.
    """
    git_dir = None
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "index"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            git_dir = completed.stdout.strip()
    except OSError, subprocess.SubprocessError:
        return None
    if not git_dir:
        return None

    with tempfile.TemporaryDirectory(prefix="gate-index-") as tmp:
        index = Path(tmp) / "index"
        source = Path(git_dir)
        if not source.is_absolute():
            source = root / git_dir
        # No index yet (a fresh repo with no staged content) is not an error:
        # start empty and let `add -A` build the whole tree.
        with contextlib.suppress(OSError):
            if source.is_file():
                shutil.copy2(source, index)
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        try:
            added = subprocess.run(
                ["git", "-C", str(root), "add", "-A", "."],
                capture_output=True,
                env=env,
                timeout=120,
                check=False,
            )
            if added.returncode != 0:
                logger.debug("gate test cache: git add -A failed: %s", added.stderr[:200])
                return None
            written = subprocess.run(
                ["git", "-C", str(root), "write-tree"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            return None
    tree = written.stdout.strip()
    return tree or None


def cache_key(tree_hash: str, test_files: Sequence[str], *, package: str) -> str:
    """Key a result by worktree tree + the exact test list that produced it.

    The test list is sorted so argument order cannot create a second entry for
    the same run, and the package directory is included because the same relative
    path means a different file under a different package root.
    """
    payload = json.dumps(
        {
            "v": _CACHE_VERSION,
            "tree": tree_hash,
            "package": package,
            "tests": sorted(test_files),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _entry_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def prune_cache(cache_dir: Path, ttl_s: int) -> int:
    """Delete entries older than *ttl_s*; return how many were removed."""
    if not cache_dir.is_dir():
        return 0
    cutoff = time.time() - ttl_s
    removed = 0
    for entry in cache_dir.glob("*.json"):
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            # A concurrent gate reaped it, or the dir moved under us: either way
            # the entry is gone, which is the outcome we wanted.
            continue
    return removed


def cache_load(cache_dir: Path, key: str, *, ttl_s: int) -> PytestResult | None:
    """Return the cached result for *key*, or ``None`` on miss / expiry / damage.

    A damaged or half-written entry is treated as a miss and dropped, never
    raised: a corrupt cache must not be able to fail a gate run.
    """
    path = _entry_path(cache_dir, key)
    try:
        stat = path.stat()
    except OSError:
        return None
    if ttl_s >= 0 and (time.time() - stat.st_mtime) > ttl_s:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        _unlink_quiet(path)
        return None
    if not isinstance(data, dict) or data.get("v") != _CACHE_VERSION:
        _unlink_quiet(path)
        return None
    try:
        returncode = int(data["rc"])
        stdout = str(data.get("stdout", ""))
        stderr = str(data.get("stderr", ""))
    except KeyError, TypeError, ValueError:
        _unlink_quiet(path)
        return None
    return PytestResult(returncode=returncode, stdout=stdout, stderr=stderr)


def cache_store(cache_dir: Path, key: str, result: PytestResult) -> bool:
    """Store *result* under *key*; return True when it was written.

    Only results that say something real about the code are stored: an exit >= 2
    is a collection or infra failure, and replaying one would let a transient
    break (a half-applied patch, a missing venv) masquerade as a settled
    verdict for a day.
    """
    if result.infra_error:
        return False
    payload = {
        "v": _CACHE_VERSION,
        "rc": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    path = _entry_path(cache_dir, key)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a reader never sees a partial entry, and a crash
        # mid-write leaves the previous entry intact rather than a truncated one.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("gate test cache: store failed: %s", exc)
        _unlink_quiet(path.with_suffix(f".{os.getpid()}.tmp"))
        return False
    return True


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
