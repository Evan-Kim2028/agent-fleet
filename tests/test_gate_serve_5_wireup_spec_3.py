"""spec-3: the documented `fleet serve --operator NAME --config fleet.yaml` form.

``docs/FLEET-SERVE.md:3`` opens with exactly that invocation. It cannot parse:
``--config`` is registered only on the top-level ``agent_fleet.cli`` parser, so
once ``serve`` has been consumed, ``--config`` is no longer a known option and
its *value* is taken as the subcommand name — argparse aborts with
``invalid choice: '<path>' (choose from run, status, stop, ...)`` and exit 2.

The shipped doc therefore contradicts the shipped CLI, and the same file at
line 366 documents why ``serve`` deliberately does not re-register ``--config``
— so the "fix" is to correct the documentation, not the registration.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from agent_fleet.serve.cli import register_serve_commands

DOC = Path("docs/FLEET-SERVE.md")


def _top_level_parser() -> argparse.ArgumentParser:
    """A parser shaped like ``agent_fleet.cli.main``: --config at top level only."""
    parser = argparse.ArgumentParser(prog="fleet", allow_abbrev=False)
    parser.add_argument("--config", default=None, help="Path to fleet.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    register_serve_commands(sub)
    return parser


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_documented_invocation_parses(tmp_path: Path) -> None:
    """`fleet serve --operator NAME --config FILE` must not be an argparse error."""
    config = tmp_path / "fleet.yaml"
    config.write_text("serve:\n  tick_seconds: 15\n", encoding="utf-8")
    parser = _top_level_parser()
    try:
        args = parser.parse_args(["serve", "--operator", "documents-0e", "--config", str(config)])
    except SystemExit as exc:
        pytest.fail(
            "the invocation documented at docs/FLEET-SERVE.md:3 exits "
            f"{exc.code}: --config is registered only on the top-level parser, so its "
            "value is consumed as the serve subcommand name"
        )
    assert args.operator == "documents-0e"
    assert args.config == str(config)


def test_the_serve_subparser_rejects_config_as_a_subcommand_name() -> None:
    """The mechanism, stated directly: the path is read as the subcommand."""
    parser = _top_level_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["serve", "--operator", "evan", "--config", "/tmp/real.yaml"])
    assert exc.value.code == 2


def test_the_documented_config_value_is_never_reaching_the_serve_namespace(
    tmp_path: Path,
) -> None:
    """A successful parse of the documented form must carry the config through.

    Once the invocation is made to parse, the value also has to arrive — a parse
    that succeeds by dropping the flag is the same failure wearing a hat.
    """
    parser = _top_level_parser()
    args = parser.parse_args(
        ["--config", str(tmp_path / "fleet.yaml"), "serve", "--operator", "evan"]
    )
    assert args.config == str(tmp_path / "fleet.yaml")


def test_the_docstring_written_form_is_the_only_working_one() -> None:
    """The working spelling is the top-level one; the doc must not teach the other.

    This pins the intent the module docstring already states: ``--config`` is
    deliberately *not* re-registered on ``serve``. The test therefore asserts the
    document and the docstring do not disagree about a form that cannot work.
    """
    text = _doc_text()
    m = re.search(r"`fleet serve [^`]*--config[^`]*`", text)
    assert m is not None, "FLEET-SERVE.md no longer shows a `fleet serve ... --config` form"
    documented = m.group(0)
    # The documented form must not put a top-level-only flag after `serve`.
    after_serve = documented.split("serve", 1)[1]
    assert "--config" not in after_serve, (
        f"the documented invocation {documented!r} cannot parse: --config is only "
        "registered on the top-level parser. Document the working form "
        "`fleet --config FILE serve --operator NAME`."
    )


def test_a_config_value_before_serve_still_parses(tmp_path: Path) -> None:
    """Control: the top-level form named in the docstring's rationale works."""
    path = tmp_path / "fleet.yaml"
    path.write_text("serve: {}\n", encoding="utf-8")
    args = _top_level_parser().parse_args(["--config", str(path), "serve", "--operator", "evan"])
    assert args.config == str(path)
    assert args.operator == "evan"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
