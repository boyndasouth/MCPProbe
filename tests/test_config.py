"""mcpprobe.toml, --dry-run, --total-timeout and several reports in one run."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from mcpprobe.cli import app
from mcpprobe.config import ConfigError, apply_checks, load_config
from mcpprobe.engine.catalog import Severity, Suite, load_catalog
from mcpprobe.engine.runner import Runner, Status
from mcpprobe.exit_codes import ExitCode
from mcpprobe.suites import SuiteOptions, collect
from tests.support.reference import REFERENCE, stdio_spec

if TYPE_CHECKING:
    from pathlib import Path

cli = CliRunner()


def _reference(*breaks: str) -> str:
    flags = " ".join(f"--break {b}" for b in breaks)
    return f'"{sys.executable}" -u "{REFERENCE}" {flags}'.strip()


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "mcpprobe.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ loading


def test_no_file_means_defaults(tmp_path: Path) -> None:
    config = load_config(None, cwd=tmp_path)
    assert config.source is None
    assert config.run.seed is None


def test_spec_example_loads_and_expands_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_KEY", "k-123")
    _write(
        tmp_path,
        """
[server]
stdio = "python -m my_server"
env = { API_KEY = "${API_KEY}" }

[run]
suites = ["conf", "fuzz", "rob", "sec"]
seed = 42
fuzz_examples_per_tool = 200
timeout_ms = 10000
allow_destructive = false

[checks]
disable = ["CONF-012"]
severity_overrides = { "SEC-002" = "info" }

[eval]
provider = "anthropic"
model = "claude-sonnet-5-5"
runs_per_task = 5
tasks = "eval/tasks.yaml"
""",
    )
    config = load_config(None, cwd=tmp_path)
    assert config.server.env == {"API_KEY": "k-123"}
    assert config.run.fuzz_examples_per_tool == 200
    assert config.checks.severity_overrides == {"SEC-002": Severity.INFO}
    assert config.eval["provider"] == "anthropic"  # accepted, used in M5


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[run]\nseeed = 1\n", "seeed"),
        ("[run]\nseed = -1\n", "seed"),
        ("[server]\nstdio = 'a'\nhttp = 'b'\n", "not both"),
        ("[server]\nstdio = '${MCPPROBE_UNSET_VARIABLE}'\n", "MCPPROBE_UNSET_VARIABLE"),
        ("not toml = = 1", "mcpprobe.toml"),
        ("[checks]\nseverity_overrides = { 'SEC-002' = 'fatal' }\n", "severity_overrides"),
    ],
)
def test_bad_files_are_refused(tmp_path: Path, text: str, match: str) -> None:
    _write(tmp_path, text)
    with pytest.raises(ConfigError, match=match):
        load_config(None, cwd=tmp_path)


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", cwd=tmp_path)


def test_checks_disable_and_override() -> None:
    catalog = load_catalog()
    from mcpprobe.config import ChecksConfig

    checks = ChecksConfig(disable=["CONF-012"], severity_overrides={"SEC-007": Severity.WARN})
    applied = {c.id: c for c in apply_checks(catalog, checks, "test")}
    assert "CONF-012" not in applied
    assert applied["SEC-007"].severity is Severity.WARN
    with pytest.raises(ConfigError, match="CONF-999"):
        apply_checks(catalog, ChecksConfig(disable=["CONF-999"]), "test")


def test_disabling_a_related_check_drops_only_that_result() -> None:
    from mcpprobe.config import ChecksConfig

    catalog = apply_checks(load_catalog(), ChecksConfig(disable=["FUZZ-003"]), "test")
    [(_, cases)] = collect([Suite.FUZZ], catalog, SuiteOptions())
    assert [r.id for r in cases[0].related] == ["FUZZ-002", "FUZZ-004"]


# ---------------------------------------------------------------------- CLI


def test_cli_reads_the_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    # A literal string ('''...''') keeps Windows backslashes as they are; in a
    # basic string ("...") TOML would read C:\Users as a \U escape.
    _write(
        tmp_path,
        f"""
[server]
stdio = '''{_reference("CONF-004")}'''

[run]
suites = ["conf"]
seed = 3
cases_dir = "cases"

[checks]
severity_overrides = {{ "CONF-004" = "warn" }}
""",
    )
    result = cli.invoke(app, ["run", "--report", "json"])
    # CONF-004 fails, but the override makes it a warning: the run passes.
    assert result.exit_code == ExitCode.PASSED, result.output
    data = json.loads(result.stdout)
    assert data["seed"] == 3
    conf_004 = next(r for r in data["results"] if r["id"] == "CONF-004")
    assert (conf_004["severity"], conf_004["status"]) == ("warn", "failed")
    # The command line beats the file.
    again = cli.invoke(app, ["run", "--seed", "9", "--report", "json"])
    assert json.loads(again.stdout)["seed"] == 9


def test_cli_invalid_config_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "[run]\nfuzz_examples = 5\n")
    result = cli.invoke(app, ["run", "--stdio", "x"])
    assert result.exit_code == ExitCode.CONFIG_INVALID
    assert "fuzz_examples" in result.output


def test_several_reports_in_one_run(tmp_path: Path) -> None:
    files = {fmt: tmp_path / f"probe.{fmt}" for fmt in ("json", "sarif", "junit", "html")}
    args = ["run", "--stdio", _reference("CONF-004"), "--suites", "conf", "--seed", "1"]
    args += ["--cases-dir", str(tmp_path / "cases")]
    for fmt, path in files.items():
        args += ["--report", fmt, "-o", str(path)]
    result = cli.invoke(app, args)
    assert result.exit_code == ExitCode.FAILED, result.output
    for path in files.values():
        assert "CONF-004" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "extra",
    [
        ["--report", "json", "--report", "sarif"],  # two file reports, no -o
        ["--report", "json", "-o", "a.json", "-o", "b.json"],  # more -o than reports
        ["--report", "json", "--report", "json", "-o", "a", "-o", "b"],  # duplicate
    ],
)
def test_bad_report_combinations_exit_3(extra: list[str]) -> None:
    assert cli.invoke(app, ["run", "--stdio", "x", *extra]).exit_code == ExitCode.CONFIG_INVALID


def test_dry_run_calls_no_tool(tmp_path: Path) -> None:
    # delete_everything exits the server when called; the run must not touch it,
    # and with --allow-destructive the dry run must still only list it.
    result = cli.invoke(app, ["run", "--stdio", _reference(), "--dry-run", "--allow-destructive"])
    assert result.exit_code == ExitCode.PASSED, result.output
    text = " ".join(result.output.split())
    assert "no tool was called" in text
    assert "delete_everything" in text
    assert "5 of 5 tool(s) would be called" in text
    default = " ".join(
        cli.invoke(app, ["run", "--stdio", _reference(), "--dry-run"]).output.split()
    )
    assert "4 of 5 tool(s) would be called" in default
    assert "may be destructive" in default


async def test_total_timeout_skips_the_rest_and_fails_the_run(tmp_path: Path) -> None:
    plan = collect(list(Suite)[:4], load_catalog(), SuiteOptions())
    runner = Runner(stdio_spec(), 1, tmp_path / "cases", total_timeout=0.5)
    result = await runner.run(plan)
    assert result.out_of_time
    assert result.exit_code is ExitCode.PROBE_ERROR
    skipped = [r for r in result.results if "total timeout" in r.message]
    assert skipped
    assert all(r.status is Status.SKIPPED for r in skipped)
    assert not any(r.status is Status.TIMEOUT for r in result.results)


async def test_total_timeout_holds_when_timers_fire_a_tick_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On Windows asyncio may fire a timer up to one clock tick (~16 ms) before
    # the deadline, so the clock still shows a little time left when the run's
    # deadline cuts a case short. That must still be "out of time", never a
    # timeout finding against the server. Simulate it on any OS.
    real = Runner._remaining

    def lagging(self: Runner) -> float | None:
        left = real(self)
        return None if left is None else max(left, 0.016)

    monkeypatch.setattr(Runner, "_remaining", lagging)
    plan = collect(list(Suite)[:4], load_catalog(), SuiteOptions())
    runner = Runner(stdio_spec(), 1, tmp_path / "cases", total_timeout=0.5)
    result = await runner.run(plan)
    assert result.out_of_time
    assert not any(r.status is Status.TIMEOUT for r in result.results)
