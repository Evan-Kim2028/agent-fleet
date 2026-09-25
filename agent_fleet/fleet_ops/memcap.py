"""Run a command under a memory cap, and scope tests to the files that changed.

Two separate problems, one module, because they share the same escape hatch.

**The cap.** A 36 GB runaway pytest on this laptop (owner-fence: "run every test
suite under a memory cap") is why every engine spawn here goes through
:func:`wrap_memory_capped`. The bash drivers did this with
``systemd-run --user --scope -q -p MemoryMax=8G -p MemorySwapMax=0``; the port
keeps the same mechanism, with ``MemorySwapMax=0`` so a capped process cannot
silently grow past its ceiling by swapping.

When systemd is not available (a container, a test run, a machine without a user
bus) the cap degrades to ``ulimit -v`` inside a shell, which is a *weaker* but
real limit. We never silently run uncapped: if neither mechanism can be used the
caller gets an error rather than an unbounded subprocess.

**The scope.** Requirement 4: repo ``verify_commands`` for lake-of-rage run
whole suites, and the pipe suite alone has produced a 36 GB / 83-minute run. So
:func:`scope_tests` derives a test selection from the branch's changed files
instead of trusting the repo's suite-level verify command. It maps each changed
source file to the test files that could possibly exercise it, and includes tests
that import the changed module.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    Runner = Callable[..., "subprocess.CompletedProcess[str]"]

logger = logging.getLogger(__name__)

#: The cap the owner mandated for every test suite (owner fence, 2026-09-24).
DEFAULT_MEMORY_MAX = "6G"

#: Devin gets a larger cap than the test cap; the bash drivers used 10G.
DEVIN_MEMORY_MAX = "10G"

#: No swap, always: a swapped-out process defeats the point of the cap.
MEMORY_SWAP_MAX = "0"


class MemoryCapError(RuntimeError):
    """Raised when no usable memory cap could be applied."""


@dataclass(frozen=True)
class CapPlan:
    """How a command will be capped, and the argv to actually execute."""

    argv: list[str]
    mechanism: str  # "systemd" | "ulimit"
    memory_max: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "argv": self.argv,
            "mechanism": self.mechanism,
            "memory_max": self.memory_max,
            "detail": self.detail,
        }


def _have_systemd_run() -> bool:
    return shutil.which("systemd-run") is not None and bool(
        os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    )


def plan_memory_cap(
    argv: Sequence[str],
    *,
    memory_max: str = DEFAULT_MEMORY_MAX,
    use_systemd: bool | None = None,
    shell: bool = False,
) -> CapPlan:
    """Build the argv that runs *argv* under a memory cap.

    *use_systemd* forces the choice; left ``None`` it probes. The fallback is
    ``sh -c 'ulimit -v …; exec …'``, which needs a shell even when the caller did
    not ask for one.
    """
    argv = [str(a) for a in argv]
    if not argv:
        raise ValueError("cannot memory-cap an empty command")

    want_systemd = _have_systemd_run() if use_systemd is None else use_systemd
    if want_systemd:
        if not shutil.which("systemd-run"):
            raise MemoryCapError("systemd-run requested but not on PATH")
        return CapPlan(
            argv=[
                "systemd-run",
                "--user",
                "--scope",
                "-q",
                "-p",
                f"MemoryMax={memory_max}",
                "-p",
                f"MemorySwapMax={MEMORY_SWAP_MAX}",
                *argv,
            ],
            mechanism="systemd",
            memory_max=memory_max,
            detail="systemd-run --user --scope",
        )

    if shell:
        inner = " ".join(_shquote(a) for a in argv)
        return CapPlan(
            argv=[
                "sh",
                "-c",
                f"ulimit -v {_ulimit_kb(memory_max)}; exec {inner}",
            ],
            mechanism="ulimit",
            memory_max=memory_max,
            detail="ulimit -v fallback (systemd user bus unavailable)",
        )

    # ulimit needs a shell, so the caller's argv is executed by one.
    inner = " ".join(_shquote(a) for a in argv)
    return CapPlan(
        argv=["sh", "-c", f"ulimit -v {_ulimit_kb(memory_max)}; exec {inner}"],
        mechanism="ulimit",
        memory_max=memory_max,
        detail="ulimit -v fallback (systemd user bus unavailable)",
    )


def _ulimit_kb(memory_max: str) -> int:
    """Convert ``6G`` / ``6144M`` / ``6291456K`` / a raw byte count to kilobytes.

    ``ulimit -v`` takes kilobytes, and the owner fence spells it as
    ``ulimit -v 6000000`` for a 6G cap.
    """
    text = str(memory_max).strip().upper()
    if not text:
        raise MemoryCapError("empty memory cap")
    units = {"K": 1, "M": 1024, "G": 1024 * 1024, "T": 1024 * 1024 * 1024}
    if text[-1] in units:
        number, unit = text[:-1], text[-1]
    elif text.endswith("B"):
        number, unit = text[:-1], "K"
    else:
        number, unit = text, "K"
    try:
        value = float(number)
    except ValueError:
        raise MemoryCapError(f"unparseable memory cap: {memory_max!r}") from None
    kb = int(value * units[unit])
    if kb <= 0:
        raise MemoryCapError(f"memory cap must be positive: {memory_max!r}")
    return kb


def _shquote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def run_memory_capped(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    memory_max: str = DEFAULT_MEMORY_MAX,
    use_systemd: bool | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 3600,
    runner: Runner | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* under a memory cap. See the module docstring.

    Raises :class:`MemoryCapError` rather than running unbounded, so a missing
    systemd bus is a visible failure instead of a surprise 36 GB run.
    """
    plan = plan_memory_cap(argv, memory_max=memory_max, use_systemd=use_systemd)
    run = runner or subprocess.run
    return run(
        plan.argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )


# ------------------------------------------------------------------ test scope


#: Test-file suffixes, per language, that a changed source file maps onto.
_TEST_SUFFIXES = (
    "test_*.py",
    "*_test.py",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.ts",
    "*.spec.js",
    "*.test.js",
)


@dataclass(frozen=True)
class TestScope:
    """Which test files a branch's changes can possibly affect."""

    test_files: tuple[Path, ...]
    changed_files: tuple[Path, ...]
    reason: str = ""

    @property
    def empty(self) -> bool:
        return not self.test_files

    def to_dict(self) -> dict[str, object]:
        return {
            "test_files": [str(p) for p in self.test_files],
            "changed_files": [str(p) for p in self.changed_files],
            "reason": self.reason,
        }

    def argv(self, *, test_command: str = "pytest") -> list[str]:
        """The scoped test argv.

        When the scope is empty we run *nothing* rather than falling back to a
        full suite: a full-suite run is exactly the 36 GB failure this scoping
        exists to prevent, and "no targeted tests" is a finding the caller
        should report, not a gap to fill with a whole-repo run.
        """
        return [test_command, *[str(p) for p in self.test_files]]


def changed_files(
    worktree: Path,
    *,
    base: str = "main",
    runner: Runner | None = None,
) -> list[Path]:
    """Files the branch touches, including uncommitted ones.

    Both sides matter: a commit the agent made and a file it left uncommitted are
    both things a test could fail on, and the guarantee step may not have run yet
    when we scope.
    """
    run = runner or subprocess.run
    results: list[Path] = []

    def _add(spec: list[str]) -> None:
        result = run(spec, cwd=worktree, capture_output=True, text=True, check=False, timeout=120)
        if result.returncode != 0:
            return
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line:
                results.append(Path(line))

    _add(["git", "diff", "--name-only", f"{base}...HEAD"])
    _add(["git", "diff", "--name-only"])
    _add(["git", "ls-files", "--others", "--exclude-standard"])

    seen: set[str] = set()
    unique: list[Path] = []
    for path in results:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def scope_tests(
    worktree: Path,
    *,
    base: str = "main",
    test_roots: Sequence[Path] | None = None,
    runner: Runner | None = None,
) -> TestScope:
    """Derive the targeted test selection for a branch's changes.

    Two inclusions, both conservative:

    * **Same-module tests** — ``pipe/catalog/foo.py`` selects ``pipe/catalog/
      test_foo.py`` and anything in a ``tests/`` tree that mentions ``foo``.
    * **Importing tests** — any discovered test file whose text imports the
      changed module. A refactor of a shared helper breaks its consumers' tests
      even though nothing in their path changed, and those are precisely the
      failures a scoped run would otherwise miss.
    """
    wt = Path(worktree)
    files = changed_files(wt, base=base, runner=runner)
    if not files:
        return TestScope((), (), reason=f"no changed files against {base}")

    roots = [Path(r) for r in (test_roots or default_test_roots(wt))]
    discovered = discover_test_files(roots)

    source_files = [f for f in files if not _looks_like_test(f)]
    stems = {f.stem for f in source_files if f.suffix in (".py", ".ts", ".tsx", ".js")}
    modules = {_module_name(f) for f in source_files}

    selected: list[Path] = []
    for test_file in discovered:
        if test_file.stem.startswith("test_"):
            target = test_file.stem[len("test_") :]
            if target in stems or f"{target}_test" in stems:
                selected.append(test_file)
                continue
        if test_file.stem.endswith("_test"):
            target = test_file.stem[: -len("_test")]
            if target in stems:
                selected.append(test_file)
                continue
        if _imports_any(test_file, modules):
            selected.append(test_file)

    return TestScope(
        test_files=tuple(dict.fromkeys(selected)),
        changed_files=tuple(files),
        reason=f"{len(source_files)} changed source file(s) against {base}",
    )


def default_test_roots(worktree: Path) -> list[Path]:
    """Conventional test roots, present or not."""
    wt = Path(worktree)
    return [
        wt / "tests",
        wt / "test",
        wt / "spec",
        wt / "api" / "tests",
        wt / "pipeline" / "tests",
        *(
            child
            for child in _safe_iterdir(wt)
            if child.name.startswith("test_") or child.name == "tests"
        ),
    ]


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        return []


def discover_test_files(roots: Sequence[Path]) -> list[Path]:
    """Every test file under *roots*. A missing root is not an error."""
    found: list[Path] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for pattern in _TEST_SUFFIXES:
            try:
                found.extend(sorted(root.rglob(pattern)))
            except OSError:
                continue
    return [p for p in dict.fromkeys(found) if p.is_file()]


def _looks_like_test(path: Path) -> bool:
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
    )


def _module_name(path: Path) -> str:
    """The dotted/stemmed name a test would import, e.g. ``pipe.catalog.foo``."""
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_any(test_file: Path, modules: set[str]) -> bool:
    """True when *test_file*'s text references any changed module.

    A substring check on the dotted module path and on the bare stem: test files
    import ``from pipe.catalog.foo import bar`` as often as they import the
    single-segment name, and a full AST parse of every test file for every changed
    file is not worth the cost of a wrong negative.
    """
    if not modules:
        return False
    try:
        text = test_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    stems = {m.rsplit(".", 1)[-1] for m in modules}
    for module in modules:
        if module in text:
            return True
    for stem in stems:
        if len(stem) < 3:
            continue
        if f"import {stem}" in text or f"from {stem}" in text or f"from .{stem}" in text:
            return True
    return False


__all__ = [
    "DEFAULT_MEMORY_MAX",
    "DEVIN_MEMORY_MAX",
    "CapPlan",
    "MemoryCapError",
    "TestScope",
    "changed_files",
    "discover_test_files",
    "plan_memory_cap",
    "run_memory_capped",
    "scope_tests",
]
