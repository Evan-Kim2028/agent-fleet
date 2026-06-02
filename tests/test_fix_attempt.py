"""Tests for the Fix Attempt memory seam (C3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent_fleet.fix_attempt import (
    ColdRestartStrategy,
    FixMemory,
    WarmContinuationStrategy,
    _FixDeps,
    make_fix_strategy,
)
from agent_fleet.runner import FleetRunConfig


def _make_deps() -> _FixDeps:
    return _FixDeps(
        backend=MagicMock(),
        persona_resolver=MagicMock(),
        fleet_config=None,
        persona="sage",
        repo_root=Path("/repo"),
        require_mcp=False,
        compose_body=None,
    )


def _make_mem(*, attempt: int = 1, failures: tuple[str, ...] = ("err",)) -> FixMemory:
    return FixMemory(
        attempt=attempt,
        diff_so_far="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new",
        failures=failures,
        files_touched=("foo.py",),
    )


class TestFixMemory:
    def test_fields(self) -> None:
        mem = FixMemory(attempt=2, diff_so_far="d", failures=("a", "b"), files_touched=("x.py",))
        assert mem.attempt == 2
        assert mem.diff_so_far == "d"
        assert mem.failures == ("a", "b")
        assert mem.files_touched == ("x.py",)


class TestMakeFixStrategy:
    def test_cold_returns_cold_strategy(self) -> None:
        s = make_fix_strategy("cold")
        assert isinstance(s, ColdRestartStrategy)

    def test_warm_returns_warm_strategy(self) -> None:
        s = make_fix_strategy("warm")
        assert isinstance(s, WarmContinuationStrategy)

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            make_fix_strategy("turbo")


class TestFleetRunConfigDefault:
    def test_default_fix_strategy_is_cold(self) -> None:
        cfg = FleetRunConfig()
        assert cfg.fix_strategy == "cold"


class TestColdRestartStrategy:
    def test_disposes_old_session_and_creates_new(self) -> None:
        old_session = MagicMock(name="old_session")
        new_session = MagicMock(name="new_session")
        mock_brief = MagicMock()
        deps = _make_deps()
        mem = _make_mem()
        task_spec = MagicMock()
        notes: list[Any] = []

        with (
            patch(
                "agent_fleet.fix_attempt.create_fleet_session", return_value=new_session
            ) as mock_create,
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief),
            patch("agent_fleet.fix_attempt.implement"),
        ):
            strategy = ColdRestartStrategy()
            returned_brief, returned_session = strategy.run_fix(
                mem,
                task_spec=task_spec,
                worktree=Path("/repo"),
                branch="fleet/sage/1-abc",
                deps=deps,
                notes=notes,
                session=old_session,
                brief=None,
            )

        old_session.dispose.assert_called_once()
        mock_create.assert_called_once()
        assert returned_session is new_session
        assert returned_brief is mock_brief

    def test_handles_dispose_exception_silently(self) -> None:
        old_session = MagicMock(name="bad_session")
        old_session.dispose.side_effect = RuntimeError("boom")
        new_session = MagicMock(name="new_session")
        mock_brief = MagicMock()
        deps = _make_deps()
        mem = _make_mem()

        with (
            patch("agent_fleet.fix_attempt.create_fleet_session", return_value=new_session),
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief),
            patch("agent_fleet.fix_attempt.implement"),
        ):
            strategy = ColdRestartStrategy()
            _, returned_session = strategy.run_fix(
                mem,
                task_spec=MagicMock(),
                worktree=Path("/repo"),
                branch="b",
                deps=deps,
                notes=[],
                session=old_session,
                brief=None,
            )

        assert returned_session is new_session

    def test_synthesize_called_with_cold_context(self) -> None:
        deps = _make_deps()
        mem = FixMemory(
            attempt=1,
            diff_so_far="",
            failures=("test failure output",),
            files_touched=(),
        )
        mock_brief = MagicMock()

        with (
            patch("agent_fleet.fix_attempt.create_fleet_session", return_value=MagicMock()),
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief) as mock_synth,
            patch("agent_fleet.fix_attempt.implement"),
        ):
            ColdRestartStrategy().run_fix(
                mem,
                task_spec=MagicMock(),
                worktree=Path("/repo"),
                branch="b",
                deps=deps,
                notes=[],
                session=None,
                brief=None,
            )

        extra_ctx = mock_synth.call_args.kwargs["extra_context"]
        assert "Verification failed:" in extra_ctx
        assert "test failure output" in extra_ctx

    def test_implement_called_with_prompt_suffix(self) -> None:
        deps = _make_deps()
        mem = FixMemory(attempt=1, diff_so_far="", failures=("fail msg",), files_touched=())
        mock_brief = MagicMock()

        with (
            patch("agent_fleet.fix_attempt.create_fleet_session", return_value=MagicMock()),
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief),
            patch("agent_fleet.fix_attempt.implement") as mock_impl,
        ):
            ColdRestartStrategy().run_fix(
                mem,
                task_spec=MagicMock(),
                worktree=Path("/repo"),
                branch="b",
                deps=deps,
                notes=[],
                session=None,
                brief=None,
            )

        suffix = mock_impl.call_args.kwargs["prompt_suffix"]
        assert "fail msg" in suffix

    def test_none_session_does_not_call_dispose(self) -> None:
        deps = _make_deps()
        mem = _make_mem()
        mock_brief = MagicMock()

        with (
            patch("agent_fleet.fix_attempt.create_fleet_session", return_value=MagicMock()),
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief),
            patch("agent_fleet.fix_attempt.implement"),
        ):
            ColdRestartStrategy().run_fix(
                mem,
                task_spec=MagicMock(),
                worktree=Path("/repo"),
                branch="b",
                deps=deps,
                notes=[],
                session=None,
                brief=None,
            )


class TestWarmContinuationStrategy:
    def test_does_not_dispose_session(self) -> None:
        existing_session = MagicMock(name="warm_session")
        deps = _make_deps()
        mem = _make_mem()
        mock_brief = MagicMock()

        with (
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief),
            patch("agent_fleet.fix_attempt.implement"),
        ):
            _, returned_session = WarmContinuationStrategy().run_fix(
                mem,
                task_spec=MagicMock(),
                worktree=Path("/repo"),
                branch="b",
                deps=deps,
                notes=[],
                session=existing_session,
                brief=None,
            )

        existing_session.dispose.assert_not_called()
        assert returned_session is existing_session

    def test_passes_failures_into_extra_context(self) -> None:
        deps = _make_deps()
        mem = FixMemory(
            attempt=2,
            diff_so_far="some diff",
            failures=("err1", "err2"),
            files_touched=("a.py", "b.py"),
        )
        mock_brief = MagicMock()

        with (
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief) as mock_synth,
            patch("agent_fleet.fix_attempt.implement"),
        ):
            WarmContinuationStrategy().run_fix(
                mem,
                task_spec=MagicMock(),
                worktree=Path("/repo"),
                branch="b",
                deps=deps,
                notes=[],
                session=None,
                brief=None,
            )

        extra_ctx = mock_synth.call_args.kwargs["extra_context"]
        assert "err1" in extra_ctx
        assert "err2" in extra_ctx
        assert "some diff" in extra_ctx
        assert "a.py" in extra_ctx

    def test_does_not_create_new_session(self) -> None:
        deps = _make_deps()
        mem = _make_mem()
        mock_brief = MagicMock()
        existing_session = MagicMock()

        with (
            patch("agent_fleet.fix_attempt.create_fleet_session") as mock_create,
            patch("agent_fleet.fix_attempt.synthesize", return_value=mock_brief),
            patch("agent_fleet.fix_attempt.implement"),
        ):
            WarmContinuationStrategy().run_fix(
                mem,
                task_spec=MagicMock(),
                worktree=Path("/repo"),
                branch="b",
                deps=deps,
                notes=[],
                session=existing_session,
                brief=None,
            )

        mock_create.assert_not_called()
