"""all-3: reading a lane log for its JSON result is not superlinear in its size.

Claim under test
----------------
``_iter_json_objects`` advances one brace and retries on a failed decode, and
``_reap_lane`` hands it the whole (never rotated, never capped) lane log. The
repro is a ``fleet lane run`` child that echoes code: a 580KB log of
``'log: {\\nsome python code line\\n' * 20000`` took 3.84s of pure CPU, while
1000 blocks (29KB) took 0.04s and 5000 (145KB) took 0.25s -- and
``'{' * 100000`` (100KB) took 3.6s, i.e. *less* for a third of the bytes, which
is the signature of a cost per brace rather than per byte.

The mechanism is the one that matters, because it is what makes the cost grow:
a failed ``raw_decode`` raises ``JSONDecodeError``, and constructing that
exception is ``O(pos)`` -- it re-walks the document counting newlines to fill
in ``lineno``/``colno``. So every ``{`` in the log paid a rescan of everything
before it, and the total is quadratic in the number of braces. This runs on the
dispatch loop's own thread once per reaped lane, so with ``--max-lanes`` children
the stalls serialise and the tick rate degrades with how verbose the agents are.

The fix is to not call the decoder where an object cannot start. After a ``{``
comes either a quoted key or the ``}`` of an empty object; a brace followed by a
newline, a comment or a statement keyword cannot open one. Those are rejected by
a character test, in constant time, and the decoder is still the only thing that
decides what is valid -- a filter, not a parser.

What is asserted here is the two things that matter together: the scan is
**linear** (the log-size ratio the claim measured, inverted), and it is still
**exactly equivalent** to the old scanner, because a reader that is fast but
drops a payload would be a worse bug than the slow one.
"""

from __future__ import annotations

import json
import time
from typing import Any

from agent_fleet.fleet_ops.dispatch import _iter_json_objects, read_lane_result

#: A block of ordinary agent log that happens to contain a brace. This is the
#: input the claim measures, and the one every real agent produces.
_BLOCK = "log: {\nsome python code line\n"

#: Guard so a regression fails as a slow test rather than hanging the suite.
_BUDGET_SECONDS = 10.0


def _scan_seconds(text: str) -> float:
    start = time.perf_counter()
    list(_iter_json_objects(text))
    return time.perf_counter() - start


def test_scanning_a_lane_log_is_linear_in_its_size() -> None:
    """10x the bytes must not cost ~100x the time.

    The old scanner was quadratic in the brace count, so the ratio between two
    log sizes is the direct evidence. Quadratic would be ~100x; linear is ~10x.
    A 20x window with a 20x ceiling leaves room for a constant factor and still
    fails loudly if the rescan-per-brace ever comes back.
    """
    small = _scan_seconds(_BLOCK * 2_000)  # ~58KB
    large = _scan_seconds(_BLOCK * 20_000)  # ~580KB

    assert small < _BUDGET_SECONDS and large < _BUDGET_SECONDS, (
        f"scanning a 580KB lane log took {large:.3f}s "
        f"(58KB took {small:.3f}s); the reaped-lane path is on the dispatch loop's "
        "own thread and lane logs are never capped"
    )
    assert large <= small * 20, (
        f"10x the bytes cost {large / max(small, 1e-9):.1f}x the time "
        f"({small:.4f}s -> {large:.4f}s). A per-brace rescan of the document makes "
        "this quadratic; reading a lane log has to be linear in its length"
    )


def test_a_brace_only_log_does_not_stall_the_reaper() -> None:
    """The degenerate case in the claim: a log that is nothing but braces."""
    text = "{" * 100_000
    elapsed = _scan_seconds(text)

    assert elapsed < _BUDGET_SECONDS, (
        f"scanning 100KB of bare braces took {elapsed:.3f}s; _reap_lane does this on "
        "the dispatch loop for every lane it reaps"
    )
    assert list(_iter_json_objects(text)) == []


def test_the_scan_still_finds_the_payload() -> None:
    """Linear is worthless if it stops finding the result.

    This is the contract ``read_lane_result`` is built on: the lane prints
    ``json.dumps(..., indent=2)``, a multi-line object, and the reader takes the
    last one carrying a ``state``.
    """
    payload = json.dumps({"state": "done", "pr": 42, "worktree": "/tmp/wt"}, indent=2)

    # detail is normalised to None when the lane reported none.
    assert read_lane_result(payload) == (42, "/tmp/wt", None), (
        "a pretty-printed lane result must still be read"
    )
    assert read_lane_result(_BLOCK * 5_000 + payload) == (42, "/tmp/wt", None), (
        "a payload at the end of a brace-heavy log must still be read; the filter "
        "must reject log chatter without rejecting the result"
    )


def test_the_scan_is_equivalent_to_a_plain_decode() -> None:
    """Every shape the old scanner accepted is still accepted, in order.

    Compared against a direct ``raw_decode`` walk of the same text, so this
    pins the *values yielded* and not merely that something came back.
    """

    def reference(text: str) -> list[Any]:
        decoder = json.JSONDecoder()
        index = 0
        length = len(text)
        out: list[Any] = []
        while index < length:
            brace = text.find("{", index)
            if brace < 0:
                return out
            try:
                value, end = decoder.raw_decode(text, brace)
            except ValueError:
                index = brace + 1
                continue
            out.append(value)
            index = end
        return out

    cases = [
        "{}",
        "{ }",
        "{\n}",
        json.dumps({"state": "done", "pr": 42}),
        json.dumps({"state": "done", "pr": 42}, indent=2),
        json.dumps({"state": "a"}) + "\n" + json.dumps({"state": "b"}),
        "no braces at all",
        "",
        # braces inside string values must not be mistaken for object starts
        '{"a": "a { brace in a string"}',
        '{"a": "escaped \\" quote with { in it"}',
        '{"a": "brace } inside"}',
        # a truncated / malformed object, with and without a payload after it
        "{ truncated",
        '{"a": 1} {"b": 2} {oops} {"c": 3}',
        _BLOCK * 200,
        _BLOCK * 100 + json.dumps({"state": "done", "pr": 9}),
        json.dumps({"state": "done", "pr": 5}) + '\n{"state": "go',
    ]

    for text in cases:
        assert list(_iter_json_objects(text)) == reference(text), (
            f"scanner diverged from a plain decode on {text[:60]!r}: got "
            f"{list(_iter_json_objects(text))!r}, expected {reference(text)!r}"
        )
