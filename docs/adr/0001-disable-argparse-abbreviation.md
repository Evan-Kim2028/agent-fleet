# ADR 0001 — Disable argparse prefix abbreviation

**Status:** Accepted  
**Date:** 2026-05-30

## Context

Python's `argparse.ArgumentParser` accepts `allow_abbrev=True` by default,
which lets callers type unambiguous prefixes of subcommand names (e.g.
`fleet doc` for `fleet doctor`).  This is convenient for interactive use but
creates an invisible second routing layer that conflicts with the keyword
router introduced in P2.

The keyword router (`normalize_argv` in `agent_fleet/cli_core.py`) is the
**single source of truth** for deciding whether an argv token is a subcommand
or a plain-text goal routed to `fleet run`.  If argparse prefix abbreviation
is also active, an ambiguous prefix (e.g. a goal that starts with "s") could
be silently misrouted to a subcommand, or argparse could raise a confusing
"ambiguous" error instead of the keyword router handling it cleanly.

## Decision

Set `allow_abbrev=False` on the top-level `ArgumentParser` in
`agent_fleet/cli.py`.

## Consequences

- Subcommand names must be typed in full (e.g. `fleet doctor`, not `fleet doc`).
- The keyword router is the only token classifier; there is no secondary
  abbreviation layer that can shadow it.
- Test coverage in `tests/test_cli_core.py` exercises the router boundary
  explicitly, including the collision case where a goal text equals a subcommand
  name (resolution: use `fleet run <goal>` as the unambiguous form).
