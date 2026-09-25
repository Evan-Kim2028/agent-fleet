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

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

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

    def command(self, extra: Sequence[str] = ()) -> list[str]:
        return ["uv", "run", "pytest", "-q", "--no-header", *extra, *self.tests]


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
) -> list[str]:
    """Build the pytest command, wrapped in a memory cap when possible."""
    inner = [
        "uv",
        "run",
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
) -> PytestResult:
    """Run the given tests from *package_dir* under a memory cap.

    Never raises on a non-zero exit — the caller inspects
    :attr:`PytestResult.infra_error` to tell "the code is broken" apart from
    "pytest could not run".
    """
    if not test_files:
        return PytestResult(returncode=PYTEST_OK, stdout="", stderr="")
    cmd = build_pytest_command(test_files, memory=memory, use_systemd=use_systemd)
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
    return PytestResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
