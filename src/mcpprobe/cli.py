"""Command-line entry point: ``mcpprobe run | replay | checks``."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from mcpprobe import __version__
from mcpprobe.engine.catalog import CatalogError, Check, Suite, load_catalog
from mcpprobe.exit_codes import ExitCode

app = typer.Typer(
    name="mcpprobe",
    help="Conformance, fuzzing and usability checks for MCP servers.",
    no_args_is_help=True,
    add_completion=False,
)
checks_app = typer.Typer(help="Browse the check catalog.", no_args_is_help=True)
app.add_typer(checks_app, name="checks")

out = Console()
err = Console(stderr=True)


class ReportFormat(StrEnum):
    TERMINAL = "terminal"
    JSON = "json"
    JUNIT = "junit"
    SARIF = "sarif"
    HTML = "html"


DEFAULT_SUITES = "conf,fuzz,rob,sec"


def _fail(message: str, code: ExitCode) -> typer.Exit:
    err.print(f"[bold red]error:[/] {message}")
    return typer.Exit(int(code))


def _load_catalog_or_exit() -> tuple[Check, ...]:
    try:
        return load_catalog()
    except CatalogError as exc:
        raise _fail(str(exc), ExitCode.CONFIG_INVALID) from exc


def _parse_suites(value: str) -> tuple[Suite, ...]:
    names = [part.strip().upper() for part in value.split(",") if part.strip()]
    try:
        suites = tuple(dict.fromkeys(Suite(name) for name in names))
    except ValueError as exc:
        valid = ", ".join(s.value.lower() for s in Suite)
        raise _fail(
            f"unknown suite in {value!r} (valid: {valid})", ExitCode.CONFIG_INVALID
        ) from exc
    if not suites:
        raise _fail("--suites must name at least one suite", ExitCode.CONFIG_INVALID)
    return suites


def _version_callback(value: bool) -> None:
    if value:
        out.print(f"mcpprobe {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """MCPProbe."""


@app.command()
def run(
    stdio: Annotated[str | None, typer.Option(help="Command that starts a stdio server.")] = None,
    http: Annotated[str | None, typer.Option(help="URL of a Streamable HTTP endpoint.")] = None,
    header: Annotated[
        list[str] | None, typer.Option(help="Extra HTTP header, 'Name: value'. Repeatable.")
    ] = None,
    suites: Annotated[str, typer.Option(help="Comma-separated suites to run.")] = DEFAULT_SUITES,
    seed: Annotated[
        int | None, typer.Option(min=0, max=2**32 - 1, help="Seed for all randomness.")
    ] = None,
    report: Annotated[ReportFormat, typer.Option(help="Report format.")] = ReportFormat.TERMINAL,
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Report file.")] = None,
    keep_going: Annotated[
        bool, typer.Option(help="Run later suites even if conformance fails.")
    ] = False,
    allow_destructive: Annotated[
        bool, typer.Option(help="Fuzz tools marked destructiveHint: true.")
    ] = False,
    baseline: Annotated[
        Path | None, typer.Option(help="Previous JSON report; fail only on new findings.")
    ] = None,
) -> None:
    """Probe a server and report findings."""
    if (stdio is None) == (http is None):
        raise _fail("pass exactly one of --stdio or --http", ExitCode.CONFIG_INVALID)
    if header and http is None:
        raise _fail("--header only applies to --http servers", ExitCode.CONFIG_INVALID)
    for item in header or []:
        if ":" not in item:
            raise _fail(f"--header {item!r} must look like 'Name: value'", ExitCode.CONFIG_INVALID)
    if baseline is not None and not baseline.is_file():
        raise _fail(f"baseline not found: {baseline}", ExitCode.CONFIG_INVALID)
    _parse_suites(suites)
    _load_catalog_or_exit()

    # M0 wires this to the runner once the wire client and recorder land.
    raise _fail("the runner is not implemented yet (milestone M0)", ExitCode.PROBE_ERROR)


@app.command()
def replay(
    case: Annotated[
        Path, typer.Argument(help="Saved case, e.g. .mcpprobe/cases/FUZZ-003-7f2a.json")
    ],
) -> None:
    """Replay a saved failing case byte for byte."""
    if not case.is_file():
        raise _fail(f"case not found: {case}", ExitCode.CONFIG_INVALID)
    raise _fail("replay is not implemented yet (milestone M0)", ExitCode.PROBE_ERROR)


@checks_app.command("list")
def checks_list(
    suite: Annotated[str | None, typer.Option(help="Only show one suite, e.g. conf.")] = None,
) -> None:
    """List every check with its severity and era."""
    catalog = _load_catalog_or_exit()
    if suite is not None:
        wanted = _parse_suites(suite)
        catalog = tuple(c for c in catalog if c.suite in wanted)
    if not catalog:
        out.print("No checks in the catalog yet.")
        return
    table = Table("ID", "Severity", "Era", "Title")
    for check in catalog:
        table.add_row(check.id, check.severity.value, check.era.value, check.title)
    out.print(table)


@checks_app.command("explain")
def checks_explain(
    check_id: Annotated[str, typer.Argument(help="Check ID, e.g. CONF-006.")],
) -> None:
    """Explain one check and link the spec section it enforces."""
    catalog = {c.id: c for c in _load_catalog_or_exit()}
    check = catalog.get(check_id.upper())
    if check is None:
        raise _fail(f"no check with id {check_id!r}", ExitCode.CONFIG_INVALID)
    out.print(f"[bold]{check.id}[/] {check.title}")
    out.print(
        f"severity: {check.severity.value}   era: {check.era.value}   "
        f"transports: {', '.join(t.value for t in check.transports)}"
    )
    out.print(f"spec: {check.spec_ref}\n")
    out.print(check.description.strip())
