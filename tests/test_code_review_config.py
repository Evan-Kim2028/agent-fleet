"""Tests for code_review auto-fix configuration."""

from __future__ import annotations

from agent_fleet.code_review.config import resolve_code_review_config
from agent_fleet.pr_loop.config import PrLoopConfig


def test_code_review_inherits_from_pr_loop() -> None:
    pr_loop = PrLoopConfig(enabled=True, fix_persona="coder", max_fix_attempts=3)
    cfg = resolve_code_review_config({}, pr_loop=pr_loop)
    assert cfg is not None
    assert cfg.auto_fix is True
    assert cfg.auto_push is True
    assert cfg.auto_pr_loop is True
    assert cfg.max_fix_attempts == 3
    assert cfg.fix_persona == "coder"


def test_code_review_explicit_override() -> None:
    pr_loop = PrLoopConfig(enabled=True)
    cfg = resolve_code_review_config(
        {"code_review": {"auto_fix": False, "auto_push": False}},
        pr_loop=pr_loop,
    )
    assert cfg is not None
    assert cfg.auto_fix is False
    assert cfg.auto_push is False


def test_code_review_disabled() -> None:
    assert resolve_code_review_config({"code_review": False}, pr_loop=None) is None
