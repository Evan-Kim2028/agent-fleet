"""Smoke test — real kimi-cli round-trip. Requires KIMI_API_KEY and kimi-cli binary."""

import os
import shutil
from pathlib import Path

import pytest

from agents.kimi import call_kimi, KIMI_BIN

_KIMI_CLI_MISSING = shutil.which(KIMI_BIN) is None and not Path(KIMI_BIN).exists()


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("KIMI_API_KEY"),
    reason="KIMI_API_KEY not set",
)
@pytest.mark.skipif(
    _KIMI_CLI_MISSING,
    reason="kimi-cli binary not found",
)
def test_kimi_returns_nonempty_response(tmp_path):
    """Verifies the full kimi-cli subprocess path works end-to-end."""
    assert Path(KIMI_BIN).exists() or KIMI_BIN == "kimi-cli", (
        f"kimi-cli binary not found at {KIMI_BIN!r}. Install kimi-cli first."
    )

    api_key = os.environ["KIMI_API_KEY"]
    result = call_kimi(
        prompt="Reply with exactly the word: PONG",
        api_key=api_key,
        work_dir=str(tmp_path),
        timeout=120,
    )

    assert result, "kimi-cli returned empty output"
    assert len(result) > 0
    print(f"\nKimi response ({len(result)} chars): {result[:200]}")
