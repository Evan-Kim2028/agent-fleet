"""correctness-2: a malformed PSI field raises out of ``read_pressure``.

``_parse_psi`` is documented as: "Unparseable input yields all-zero rather than
raising: a malformed document is a broken source, and ``PressureReading.ok`` is
where that fact gets recorded." The loop that tokenises the document calls
``float(value)`` on every ``key=value`` pair, with no try/except, so a
non-numeric field raises ``ValueError`` and the promise is broken.

``_read_optional_psi`` only catches ``OSError``, so the ``ValueError`` passes
straight through ``read_pressure`` and out to the caller. The capacity loop's
input read can therefore raise, and the flag that exists precisely to tell an
unreadable source from an idle machine is never set — the module's central
"a missing file is not an idle machine" guarantee cannot be honoured for a
truncated or kernel-formatted-differently ``cpu.pressure``.

Note this is specifically about a *present but malformed* file. A missing file
already returns ``ok=False`` correctly, and that path is covered elsewhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_fleet.serve.pressure import _parse_psi, read_pressure

if TYPE_CHECKING:
    from pathlib import Path

MALFORMED = "some avg10=0.00 avg60=oops avg300=0.00 total=5\n"


def test_parse_psi_returns_all_zero_for_a_malformed_field() -> None:
    parsed = _parse_psi(MALFORMED)
    assert parsed.some_avg60 == 0.0
    assert parsed.some_total_us == 5, "the fields that *did* parse should survive"


def test_read_pressure_reports_a_malformed_document_as_a_failed_read(tmp_path: Path) -> None:
    (tmp_path / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    cg = tmp_path / "agents.slice"
    cg.mkdir()
    (cg / "cpu.pressure").write_text(MALFORMED, encoding="utf-8")
    (cg / "memory.current").write_text("1", encoding="utf-8")
    (cg / "memory.max").write_text("10", encoding="utf-8")

    reading = read_pressure("agents.slice", root=tmp_path)

    assert reading.ok is False, (
        "a cpu.pressure that cannot be parsed is a broken source, not an idle "
        "machine; PressureReading.ok is where that fact must be recorded"
    )
    assert reading.error, "a failed read must say why"
    assert reading.source_ok is False
