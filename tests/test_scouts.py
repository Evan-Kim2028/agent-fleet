"""Tests for Fleet Scouts JSON parsing."""

from __future__ import annotations

import pytest

from agent_fleet.scouts.runner import _parse_json_object


def test_parse_json_object_extracts_embedded_object() -> None:
    text = 'Here is the brief:\n{"repo": "demo", "summary": "ok"}'
    parsed = _parse_json_object(text)
    assert parsed["repo"] == "demo"


def test_parse_json_object_missing_json() -> None:
    with pytest.raises(ValueError, match="no JSON"):
        _parse_json_object("no structured output")
