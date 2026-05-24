"""Verify runner opens one session per task and routes all phase prompts through it."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_fleet.cursor_backend import CursorLLMResult


class FakeBackend:
    """Backend exposing create_session — runner should detect it and use sessions."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.agent_id = "agent-test"
        self.session.send.return_value = CursorLLMResult(
            stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id="agent-test"
        )
        self.create_session_calls: list[dict] = []

    def create_session(self, **kwargs):  # noqa: ANN003, ANN201
        self.create_session_calls.append(kwargs)
        return self.session

    def run(self, *_args: object, **_kwargs: object) -> CursorLLMResult:
        raise AssertionError("FakeBackend should route through session.send(), not run()")


def test_runner_simple_pipeline_uses_one_session(
    fleet_config_with_session_backend,  # noqa: ANN001
) -> None:
    """For the simple pipeline (single execute phase), runner must:
    1) call backend.create_session() exactly once
    2) call session.send() exactly once
    3) call session.dispose() in the finally block
    4) NOT call backend.run() at all
    """
    backend = FakeBackend()
    runner = fleet_config_with_session_backend(backend)
    runner.run(task_id=1, pipeline="simple")
    assert len(backend.create_session_calls) == 1
    assert backend.session.send.call_count == 1
    assert backend.session.dispose.call_count == 1


def test_runner_falls_back_to_backend_run_when_no_create_session(
    fleet_config_with_session_backend,  # noqa: ANN001
) -> None:
    """Backends without create_session() must still work (legacy path)."""
    legacy = MagicMock(spec=["run"])  # no create_session attribute
    legacy.run.return_value = CursorLLMResult(
        stdout="ok", stderr="", exit_code=0, duration_s=0.1, agent_id=None
    )
    runner = fleet_config_with_session_backend(legacy)
    runner.run(task_id=1, pipeline="simple")
    assert legacy.run.call_count >= 1
    assert not hasattr(legacy, "create_session")
    # No session dispose should be called since no session was created
    assert not hasattr(legacy, "session")
