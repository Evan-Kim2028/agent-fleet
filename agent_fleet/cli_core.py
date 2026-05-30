"""Pure argv normalization — the seam between shell invocation and argparse.

``normalize_argv`` is intentionally a pure function: no I/O, no imports of
heavy modules, no side effects.  This keeps it cheap to unit-test and safe to
call before the argparse parser is constructed.

Routing rules (applied in order):
  1. Empty argv               → ["summon"]  (bare invocation)
  2. First token starts '-'   → passthrough (flag-first; let argparse handle it)
  3. First token in known_subcommands → passthrough (already addressed correctly)
  4. Otherwise                → prepend "run" (treat first token as a task goal)

Rule 4 implements the "keyword router" contract: the only way to dispatch a
plain-text goal is by prepending "run", which makes ``fleet run`` the single
source of truth for goal dispatch.  When a goal text happens to equal a
subcommand name the explicit form ``fleet run <goal>`` is required (documented
in tests/test_cli_core.py and ADR 0001).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def normalize_argv(
    raw_argv: list[str],
    known_subcommands: set[str] | frozenset[str],
    cwd: Path,  # noqa: ARG001 — reserved for future locality-based routing
) -> list[str]:
    """Return a normalized argv ready to be handed to the argparse parser.

    Parameters
    ----------
    raw_argv:
        sys.argv[1:] (or equivalent).  Must not include the program name.
    known_subcommands:
        The complete set of subcommand names registered on the top-level
        argparse subparser.  Derived from ``sub.choices`` inside ``main``.
    cwd:
        Current working directory at invocation time.  Reserved for future
        locality-aware routing; not used today.

    Returns
    -------
    list[str]
        Possibly-mutated argv.  The original list is never modified.
    """
    if not raw_argv:
        return ["summon"]

    first = raw_argv[0]

    # Flag-first: let the parser handle --help, --version, --config, etc.
    if first.startswith("-"):
        return list(raw_argv)

    # Known subcommand: passthrough.
    if first in known_subcommands:
        return list(raw_argv)

    # Unknown non-flag token: route to "run".
    return ["run", *raw_argv]
