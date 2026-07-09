"""Tests for autonomy.parse_review (parity with review_parse)."""

from __future__ import annotations

from agent_fleet.autonomy import body_is_blocking, parse_review_body, review_is_blocking
from agent_fleet.pr_loop.review_parse import has_blocking_findings

SAMPLE_REVIEW = """\
## 🤖 Composer PR Analysis

**Risk Level:** 🟡 MEDIUM
<details open>
<summary>🟡 <b>MEDIUM</b> (2)</summary>
| # | Area | Finding |
|---|------|---------|
| 1 | ⚙️ backend | Missing test coverage |
</details>
"""

LOW_REVIEW = "**Risk Level:** 🟢 LOW\n<details><summary><b>LOW</b> (1)</summary>"

HIGH_COUNTS = """\
**Risk Level:** 🟢 LOW
<details><summary>🔴 <b>HIGH</b> (1)</summary>
</details>
"""


def test_parse_risk_level_medium() -> None:
    ev = parse_review_body(SAMPLE_REVIEW, head_sha="deadbeef")
    assert ev.overall_risk == "MEDIUM"
    assert ev.head_sha == "deadbeef"
    assert ev.raw_marker == "**Risk Level:**"
    assert any(f.severity == "MEDIUM" and f.count == 2 for f in ev.findings)


def test_parse_low_no_blocking_findings() -> None:
    ev = parse_review_body(LOW_REVIEW)
    assert ev.overall_risk == "LOW"
    assert review_is_blocking(ev) is False


def test_parse_high_count_blocks_even_if_overall_low() -> None:
    ev = parse_review_body(HIGH_COUNTS)
    assert ev.overall_risk == "LOW"
    assert review_is_blocking(ev) is True


def test_has_blocking_parity_with_review_parse() -> None:
    bodies = [
        SAMPLE_REVIEW,
        LOW_REVIEW,
        HIGH_COUNTS,
        "**Risk Level:** CRITICAL\n",
        "no risk line here",
        "**Risk Level:** 🟠 HIGH\n<details><summary><b>CRITICAL</b> (0)</summary>",
    ]
    for body in bodies:
        legacy = has_blocking_findings(body)
        modern = body_is_blocking(body)
        assert modern is legacy, f"parity fail for body={body!r}: {modern=} {legacy=}"


def test_deletion_only_never_blocks() -> None:
    assert body_is_blocking(SAMPLE_REVIEW, deletion_only=True) is False
    assert has_blocking_findings(SAMPLE_REVIEW, deletion_only=True) is False


def test_zero_count_bucket_does_not_block_via_counts_only() -> None:
    # overall HIGH still blocks via risk line
    body = "**Risk Level:** LOW\n<details><summary><b>MEDIUM</b> (0)</summary>"
    assert has_blocking_findings(body) is False
    assert body_is_blocking(body) is False
