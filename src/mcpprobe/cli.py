"""Command-line entry point: ``mcpprobe run | record | replay | checks``."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from mcpprobe import __version__
from mcpprobe.engine.catalog import CatalogError, Check, Suite, Transport, load_catalog
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
    env: Annotated[
        list[str] | None, typer.Option(help="Env var for a stdio server, KEY=VALUE. Masked.")
    ] = None,
    timeout: Annotated[float, typer.Option(min=0.1, help="Seconds allowed per case.")] = 10.0,
    cases_dir: Annotated[
        Path, typer.Option(help="Where failing cases are saved for replay.")
    ] = Path(".mcpprobe/cases"),
) -> None:
    """Probe a server and report findings."""
    if (stdio is None) == (http is None):
        raise _fail("pass exactly one of --stdio or --http", ExitCode.CONFIG_INVALID)
    if header and http is None:
        raise _fail("--header only applies to --http servers", ExitCode.CONFIG_INVALID)
    if env and stdio is None:
        raise _fail("--env only applies to --stdio servers", ExitCode.CONFIG_INVALID)
    headers = _parse_headers(header)
    env_vars = _parse_env(env)
    if baseline is not None and not baseline.is_file():
        raise _fail(f"baseline not found: {baseline}", ExitCode.CONFIG_INVALID)
    if report not in (ReportFormat.TERMINAL, ReportFormat.JSON) or baseline is not None:
        raise _fail(
            "only terminal and JSON reports exist so far; "
            "JUnit, SARIF, HTML and --baseline arrive in M4",
            ExitCode.CONFIG_INVALID,
        )
    if output is not None and report is ReportFormat.TERMINAL:
        raise _fail("-o needs a file format, e.g. --report json", ExitCode.CONFIG_INVALID)
    wanted = _parse_suites(suites)
    catalog = _load_catalog_or_exit()

    from mcpprobe.engine.connection import HttpServer, ServerSpec, StdioServer
    from mcpprobe.engine.runner import Runner, ServerUnavailable
    from mcpprobe.engine.seed import new_seed
    from mcpprobe.report.terminal import render
    from mcpprobe.suites import SuiteOptions, collect
    from mcpprobe.transport.process import split_command

    spec: ServerSpec
    if stdio is not None:
        argv = split_command(stdio)
        if not argv:
            raise _fail("--stdio command is empty", ExitCode.CONFIG_INVALID)
        spec = StdioServer(tuple(argv), env_vars)
    else:
        assert http is not None
        spec = HttpServer(http, tuple(headers))

    run_seed = new_seed() if seed is None else seed
    transport = Transport.STDIO if stdio is not None else Transport.HTTP
    plan = collect(
        wanted,
        catalog,
        SuiteOptions(allow_destructive=allow_destructive, transport=transport),
    )
    runner = Runner(spec, run_seed, cases_dir, case_timeout=timeout, keep_going=keep_going)
    try:
        result = asyncio.run(runner.run(plan))
    except ServerUnavailable as exc:
        raise _fail(str(exc), ExitCode.PROBE_ERROR) from exc
    if report is ReportFormat.JSON:
        from mcpprobe.report.json_report import dumps

        text = dumps(result, spec)
        if output is None:
            typer.echo(text, nl=False)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
            render(result, err, cases_dir=cases_dir)
    else:
        render(result, out, cases_dir=cases_dir)
    raise typer.Exit(int(result.exit_code))


def _parse_headers(items: list[str] | None) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for item in items or []:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise _fail(f"--header {item!r} must look like 'Name: value'", ExitCode.CONFIG_INVALID)
        headers.append((name.strip(), value.strip()))
    return headers


def _parse_env(items: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise _fail(f"--env {item!r} must look like KEY=VALUE", ExitCode.CONFIG_INVALID)
        env[key] = value
    return env


def _read_messages(path: Path) -> list[bytes]:
    if not path.is_file():
        raise _fail(f"messages file not found: {path}", ExitCode.CONFIG_INVALID)
    lines = path.read_bytes().splitlines()
    messages = [line for line in lines if line.strip() and not line.lstrip().startswith(b"#")]
    if not messages:
        raise _fail(
            f"{path} has no messages (one JSON-RPC message per line)", ExitCode.CONFIG_INVALID
        )
    return messages


@app.command()
def record(
    send: Annotated[
        Path, typer.Option(help="File with one JSON-RPC message per line, sent as-is.")
    ],
    output: Annotated[Path, typer.Option("-o", "--output", help="Where to save the recording.")],
    stdio: Annotated[str | None, typer.Option(help="Command that starts a stdio server.")] = None,
    http: Annotated[str | None, typer.Option(help="URL of a Streamable HTTP endpoint.")] = None,
    header: Annotated[
        list[str] | None, typer.Option(help="Extra HTTP header, 'Name: value'. Repeatable.")
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option(help="Env var for a stdio server, KEY=VALUE. Masked.")
    ] = None,
    derive_headers: Annotated[
        bool,
        typer.Option(help="Add MCP-Protocol-Version, Mcp-Method and Mcp-Name from each body."),
    ] = True,
    timeout: Annotated[float, typer.Option(min=0.1, help="Seconds to wait per response.")] = 10.0,
) -> None:
    """Send scripted messages to a server and save every byte (developer tool)."""
    from mcpprobe.engine.record import record_http, record_stdio
    from mcpprobe.transport.errors import TransportError
    from mcpprobe.transport.frames import StdioFrame
    from mcpprobe.transport.process import split_command

    if (stdio is None) == (http is None):
        raise _fail("pass exactly one of --stdio or --http", ExitCode.CONFIG_INVALID)
    if header and http is None:
        raise _fail("--header only applies to --http servers", ExitCode.CONFIG_INVALID)
    if env and stdio is None:
        raise _fail("--env only applies to --stdio servers", ExitCode.CONFIG_INVALID)
    headers = _parse_headers(header)
    env_vars = _parse_env(env)
    messages = _read_messages(send)

    try:
        if stdio is not None:
            argv = split_command(stdio)
            if not argv:
                raise _fail("--stdio command is empty", ExitCode.CONFIG_INVALID)
            result = asyncio.run(record_stdio(argv, messages, env=env_vars, timeout=timeout))
        else:
            assert http is not None
            result = asyncio.run(
                record_http(
                    http, messages, headers=headers, derive_headers=derive_headers, timeout=timeout
                )
            )
    except TransportError as exc:
        raise _fail(str(exc), ExitCode.PROBE_ERROR) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.recording.dumps(), encoding="utf-8")
    out.print(
        f"sent {result.sent}, answered {result.answered}, "
        f"{len(result.recording.frames)} frames saved to {output}"
    )
    for problem in result.problems:
        err.print(f"[yellow]warning:[/] {problem}")
    if result.sent and not result.answered and result.problems:
        stderr_lines = [
            f.data.decode("utf-8", "replace").rstrip()
            for f in result.recording.frames
            if isinstance(f, StdioFrame) and f.stream == "stderr"
        ]
        err.print("[yellow]the server answered nothing.[/] Its stderr said:", highlight=False)
        for line in stderr_lines[-10:] or ["(nothing)"]:
            err.print(f"  {line}", markup=False, highlight=False)


@app.command()
def replay(
    case: Annotated[
        Path, typer.Argument(help="Saved case, e.g. .mcpprobe/cases/FUZZ-003-7f2a.json")
    ],
    header: Annotated[
        list[str] | None,
        typer.Option(help="Value for a header masked in the recording. Repeatable."),
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option(help="Value for an env var the server needs, KEY=VALUE.")
    ] = None,
    timeout: Annotated[float, typer.Option(min=0.1, help="Seconds to wait per step.")] = 10.0,
    output: Annotated[
        Path | None, typer.Option("-o", "--output", help="Save the replayed session here.")
    ] = None,
) -> None:
    """Replay a saved case byte for byte and report any difference."""
    from pydantic import ValidationError

    from mcpprobe.engine.replay import ReplayError
    from mcpprobe.engine.replay import replay as replay_recording
    from mcpprobe.transport.errors import TransportError
    from mcpprobe.transport.frames import Recording

    if not case.is_file():
        raise _fail(f"case not found: {case}", ExitCode.CONFIG_INVALID)
    try:
        recording = Recording.loads(case.read_bytes())
    except ValidationError as exc:
        raise _fail(f"{case} is not a valid recording: {exc}", ExitCode.CONFIG_INVALID) from exc
    headers = _parse_headers(header)
    env_vars = _parse_env(env)

    try:
        result = asyncio.run(
            replay_recording(recording, headers=headers, env=env_vars, timeout=timeout)
        )
    except ReplayError as exc:
        raise _fail(str(exc), ExitCode.CONFIG_INVALID) from exc
    except TransportError as exc:
        raise _fail(str(exc), ExitCode.PROBE_ERROR) from exc

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.replayed.dumps(), encoding="utf-8")
    if result.matched and result.silent:
        err.print(
            "[yellow]replay matched, but only trivially[/]: the server sent no output in "
            "the original recording, so there was nothing to compare. Check the server "
            "starts and answers (see the recording's stderr frames)."
        )
        raise typer.Exit(int(ExitCode.FAILED))
    if result.matched:
        out.print(f"[green]replay matched[/]: {len(result.replayed.frames)} frames")
        return
    err.print(f"[red]replay differs[/] in {len(result.differences)} place(s):")
    for difference in result.differences:
        err.print(f"  - {difference}", markup=False, highlight=False)
    raise typer.Exit(int(ExitCode.FAILED))


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
