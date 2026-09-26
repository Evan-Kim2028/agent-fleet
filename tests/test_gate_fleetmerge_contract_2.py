"""`fleet merge holds` and `fleet merge release` must report a malformed
``merge_plan.executor`` block, not traceback.

``merge_plan.executor`` is validated strictly (unknown keys are a hard error so
a typo surfaces immediately).  ``load_executor_spec`` therefore raises
``ValueError`` for a mistyped key.  Every ``fleet merge`` subcommand that reads
the executor spec has to convert that into the same clean ``error: ...`` on
stderr plus exit 2 that ``fleet merge run`` already promises.  This drives the
real top-level CLI so the whole path -- argparse dispatch, ``_spec()``,
``load_executor_spec``, ``parse_executor_spec`` -- is exercised.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from agent_fleet.cli import main


def _write_bad_config(tmp_path: Path) -> str:
    path = tmp_path / "fleet.yaml"  # type: ignore[operator]
    path.write_text(
        "merge_plan:\n  executor:\n    post_merge_hold_second: 300\n",  # typo: missing trailing 's'
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        (["merge", "holds"], "holds"),
        (["merge", "release", "backend"], "release"),
        (["merge", "run", "--repo-path", "."], "run"),
    ],
)
def test_malformed_executor_block_reports_error_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], label: str
) -> None:
    config = _write_bad_config(tmp_path)
    args = [*argv, "--config", config]

    raised: BaseException | None = None
    try:
        code = main(args)
    except BaseException as exc:
        raised = exc
        code = None

    out = capsys.readouterr()  # type: ignore[attr-defined]
    combined = out.out + out.err

    assert raised is None, (
        f"fleet merge {label} raised {type(raised).__name__} on a malformed "
        f"merge_plan.executor block instead of reporting it:\n{combined}"
    )
    assert code == 2, f"fleet merge {label} exited {code!r}, expected 2 (output: {combined!r})"
    assert "error:" in out.err, (
        f"fleet merge {label} did not print an 'error:' line on stderr (output: {combined!r})"
    )
    assert "post_merge_hold_second" in out.err, (
        f"fleet merge {label} error did not name the offending key: {out.err!r}"
    )
    assert "Traceback" not in combined, f"fleet merge {label} leaked a traceback: {combined!r}"
