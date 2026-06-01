"""Guard against fleet entrypoints running from a non-canonical checkout."""

from pathlib import Path

import pytest

import agents._canonical as _canonical


def test_canonical_checkout_passes_under_canonical_root(monkeypatch):
    monkeypatch.setattr(
        _canonical, "CANONICAL_ROOT",
        Path(_canonical.__file__).resolve().parent.parent,
    )
    _canonical.assert_canonical_checkout()


def test_canonical_checkout_refuses_non_canonical(monkeypatch, capsys):
    monkeypatch.setattr(
        _canonical, "CANONICAL_ROOT",
        Path("/some/other/clone/agents").resolve(),
    )
    with pytest.raises(SystemExit) as exc:
        _canonical.assert_canonical_checkout()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "non-canonical checkout" in err
    assert "agent-fleet-watch.service" in err
