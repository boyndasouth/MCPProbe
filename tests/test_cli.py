from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcpprobe.cli import app
from mcpprobe.exit_codes import ExitCode

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "mcpprobe" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "--stdio", "x", "--http", "https://localhost/mcp"],
        ["run", "--stdio", "x", "--header", "A: b"],
        ["run", "--http", "https://localhost/mcp", "--header", "no-colon"],
        ["run", "--stdio", "x", "--suites", "conf,bogus"],
        ["run", "--stdio", "x", "--suites", " , "],
        ["run", "--stdio", "x", "--baseline", "missing.json"],
        ["replay", "missing.json"],
    ],
)
def test_invalid_invocations_exit_3(args: list[str]) -> None:
    assert runner.invoke(app, args).exit_code == ExitCode.CONFIG_INVALID


def test_run_reports_probe_error_until_runner_exists() -> None:
    result = runner.invoke(app, ["run", "--stdio", "python -m server", "--seed", "42"])
    assert result.exit_code == ExitCode.PROBE_ERROR


def test_replay_reports_probe_error_until_runner_exists(tmp_path: Path) -> None:
    case = tmp_path / "case.json"
    case.write_text("{}", encoding="utf-8")
    assert runner.invoke(app, ["replay", str(case)]).exit_code == ExitCode.PROBE_ERROR


def test_checks_list_and_explain_on_empty_catalog() -> None:
    listed = runner.invoke(app, ["checks", "list"])
    assert listed.exit_code == 0
    assert runner.invoke(app, ["checks", "explain", "CONF-001"]).exit_code == 3


def test_checks_list_and_explain_with_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpprobe import cli
    from mcpprobe.engine import catalog

    data = Path(__file__).parent / "data" / "checks" / "valid"
    monkeypatch.setattr(cli, "load_catalog", lambda: catalog.load_catalog(data))

    listed = runner.invoke(app, ["checks", "list", "--suite", "sec"])
    assert listed.exit_code == 0
    assert "SEC-901" in listed.output
    assert "CONF-901" not in listed.output

    explained = runner.invoke(app, ["checks", "explain", "conf-901"])
    assert explained.exit_code == 0
    assert "modelcontextprotocol.io" in explained.output


def test_broken_catalog_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpprobe import cli
    from mcpprobe.engine import catalog

    data = Path(__file__).parent / "data" / "checks" / "invalid"
    monkeypatch.setattr(cli, "load_catalog", lambda: catalog.load_catalog(data))
    assert runner.invoke(app, ["checks", "list"]).exit_code == ExitCode.CONFIG_INVALID
