"""Command-line entry point: ``mcpprobe run | record | replay | checks``."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from mcpprobe import __version__
from mcpprobe.engine.catalog import CatalogError, Check, Suite, Transport, load_catalog
from mcpprobe.exit_codes import ExitCode

if TYPE_CHECKING:
    from mcpprobe.engine.connection import ServerSpec
    from mcpprobe.engine.runner import Runner, RunResult

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
    config: Annotated[
        Path | None,
        typer.Option(help="Config file (default: ./mcpprobe.toml if it exists)."),
    ] = None,
    suites: Annotated[
        str | None, typer.Option(help=f"Comma-separated suites to run [default: {DEFAULT_SUITES}].")
    ] = None,
    seed: Annotated[
        int | None, typer.Option(min=0, max=2**32 - 1, help="Seed for all randomness.")
    ] = None,
    report: Annotated[
        list[ReportFormat] | None,
        typer.Option(help="Report format; repeat for several (each file one needs an -o)."),
    ] = None,
    output: Annotated[
        list[Path] | None,
        typer.Option("-o", "--output", help="Report file, in the order of the --report options."),
    ] = None,
    keep_going: Annotated[
        bool, typer.Option(help="Run later suites even if conformance fails.")
    ] = False,
    allow_destructive: Annotated[
        bool,
        typer.Option(help="Also call tools not marked readOnlyHint or destructiveHint: false."),
    ] = False,
    allow_open_world: Annotated[
        bool,
        typer.Option(help="Also call tools marked openWorldHint: true (they reach other hosts)."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(help="List the server's tools and which would be called; call none."),
    ] = False,
    examples: Annotated[
        int | None,
        typer.Option(
            min=1, max=10_000, help="Generated argument sets per tool (FUZZ) [default: 50]."
        ),
    ] = None,
    concurrency: Annotated[
        int | None,
        typer.Option(min=2, max=256, help="Requests in flight at once (ROB-007) [default: 8]."),
    ] = None,
    baseline: Annotated[
        Path | None, typer.Option(help="Previous JSON report; fail only on new findings.")
    ] = None,
    env: Annotated[
        list[str] | None, typer.Option(help="Env var for a stdio server, KEY=VALUE. Masked.")
    ] = None,
    timeout: Annotated[
        float | None, typer.Option(min=0.1, help="Seconds allowed per case [default: 10].")
    ] = None,
    startup_timeout: Annotated[
        float | None,
        typer.Option(
            min=0.1,
            help="Seconds for the server's first answer (npx/uvx start-up) [default: 30].",
        ),
    ] = None,
    total_timeout: Annotated[
        float | None,
        typer.Option(min=0, help="Seconds for the whole run; 0 means no limit [default: 1800]."),
    ] = None,
    cases_dir: Annotated[
        Path | None,
        typer.Option(help="Where failing cases are saved for replay [default: .mcpprobe/cases]."),
    ] = None,
    sarif_path: Annotated[
        str | None,
        typer.Option(
            help="Repository file SARIF alerts point at (default: the server's entry file)."
        ),
    ] = None,
) -> None:
    """Probe a server and report findings."""
    from mcpprobe.config import ConfigError, apply_checks, load_config

    try:
        cfg = load_config(config, cwd=Path.cwd())
    except ConfigError as exc:
        raise _fail(str(exc), ExitCode.CONFIG_INVALID) from exc
    source = cfg.source or "config"
    # Command line beats the config file; the file beats the defaults.
    if stdio is None and http is None:
        stdio, http = cfg.server.stdio, cfg.server.http
        config_headers = [f"{k}: {v}" for k, v in cfg.server.headers.items()]
        config_env = [f"{k}={v}" for k, v in cfg.server.env.items()]
        header = [*config_headers, *(header or [])] if http is not None else header
        env = [*config_env, *(env or [])] if stdio is not None else env
    if (stdio is None) == (http is None):
        raise _fail(
            "pass exactly one of --stdio or --http (or set one in mcpprobe.toml [server])",
            ExitCode.CONFIG_INVALID,
        )
    if header and http is None:
        raise _fail("--header only applies to --http servers", ExitCode.CONFIG_INVALID)
    if env and stdio is None:
        raise _fail("--env only applies to --stdio servers", ExitCode.CONFIG_INVALID)
    headers = _parse_headers(header)
    env_vars = _parse_env(env)
    run_cfg = cfg.run
    suites = suites or (",".join(run_cfg.suites) if run_cfg.suites else DEFAULT_SUITES)
    seed = seed if seed is not None else run_cfg.seed
    examples = examples or run_cfg.fuzz_examples_per_tool or 50
    concurrency = concurrency or run_cfg.concurrency or 8
    timeout = timeout or (run_cfg.timeout_ms / 1000 if run_cfg.timeout_ms else 10.0)
    startup_timeout = startup_timeout or (
        run_cfg.startup_timeout_ms / 1000 if run_cfg.startup_timeout_ms else 30.0
    )
    if total_timeout is None:
        total_timeout = (
            run_cfg.total_timeout_ms / 1000 if run_cfg.total_timeout_ms is not None else 1800.0
        )
    allow_destructive = allow_destructive or bool(run_cfg.allow_destructive)
    allow_open_world = allow_open_world or bool(run_cfg.allow_open_world)
    keep_going = keep_going or bool(run_cfg.keep_going)
    cases_dir = cases_dir or Path(run_cfg.cases_dir or ".mcpprobe/cases")
    baseline = baseline or (Path(run_cfg.baseline) if run_cfg.baseline else None)

    reports = report or [ReportFormat.TERMINAL]
    files = output or []
    file_reports = [r for r in reports if r is not ReportFormat.TERMINAL]
    if len(set(reports)) != len(reports):
        raise _fail("each --report format may be given once", ExitCode.CONFIG_INVALID)
    if files and len(files) != len(file_reports):
        raise _fail(
            f"{len(file_reports)} file report(s) but {len(files)} -o option(s); give one -o per "
            "non-terminal --report, in the same order",
            ExitCode.CONFIG_INVALID,
        )
    if not files and len(file_reports) > 1:
        raise _fail("several file reports need one -o each", ExitCode.CONFIG_INVALID)
    if files and not file_reports:
        raise _fail("-o needs a file format, e.g. --report json", ExitCode.CONFIG_INVALID)
    if sarif_path is not None and ReportFormat.SARIF not in reports:
        raise _fail("--sarif-path only applies to --report sarif", ExitCode.CONFIG_INVALID)

    known: frozenset[str] | None = None
    if baseline is not None:
        from mcpprobe.report.baseline import BaselineError, load_baseline

        if not baseline.is_file():
            raise _fail(f"baseline not found: {baseline}", ExitCode.CONFIG_INVALID)
        try:
            known = load_baseline(baseline)
        except BaselineError as exc:
            raise _fail(str(exc), ExitCode.CONFIG_INVALID) from exc
    wanted = _parse_suites(suites)
    try:
        catalog = apply_checks(_load_catalog_or_exit(), cfg.checks, source)
    except ConfigError as exc:
        raise _fail(str(exc), ExitCode.CONFIG_INVALID) from exc

    from mcpprobe.engine.connection import HttpServer, StdioServer
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
    runner = Runner(
        spec,
        run_seed,
        cases_dir,
        case_timeout=timeout,
        keep_going=keep_going,
        allow_destructive=allow_destructive,
        allow_open_world=allow_open_world,
        fuzz_examples=examples,
        concurrency=concurrency,
        startup_timeout=startup_timeout,
        total_timeout=total_timeout or None,
        baseline=known,
    )
    if dry_run:
        _dry_run(runner, allow_destructive, allow_open_world)
        raise typer.Exit(int(ExitCode.PASSED))
    try:
        result = asyncio.run(runner.run(plan))
    except ServerUnavailable as exc:
        raise _fail(str(exc), ExitCode.PROBE_ERROR) from exc
    summary = out
    if file_reports:
        summary = err  # stdout may carry a report; the summary goes to stderr
        for index, fmt in enumerate(file_reports):
            text = _format_report(fmt, result, spec, catalog, cases_dir, sarif_path)
            if files:
                files[index].parent.mkdir(parents=True, exist_ok=True)
                files[index].write_text(text, encoding="utf-8")
            else:
                typer.echo(text, nl=False)
    render(result, summary, cases_dir=cases_dir)
    if result.out_of_time:
        err.print(
            f"[bold red]incomplete:[/] the total timeout ({total_timeout:g}s) ran out; "
            "raise --total-timeout to run every case"
        )
    raise typer.Exit(int(result.exit_code))


def _dry_run(runner: Runner, allow_destructive: bool, allow_open_world: bool) -> None:
    """Print the era and which tools a run would call, without calling any."""
    from mcpprobe.engine.runner import ServerUnavailable
    from mcpprobe.suites.conf.tools import unsafe_reason

    try:
        facts, tools = asyncio.run(runner.inventory())
    except ServerUnavailable as exc:
        raise _fail(str(exc), ExitCode.PROBE_ERROR) from exc
    out.print(f"era: {facts.era.value}", highlight=False)
    if isinstance(tools, str):
        out.print(f"no tools: {tools}", highlight=False)
        return
    table = Table("tool", "would be called", "why", title="Dry run: no tool was called")
    called = 0
    for tool in tools:
        reason = unsafe_reason(
            tool, allow_destructive=allow_destructive, allow_open_world=allow_open_world
        )
        if reason is None:
            called += 1
        name = str(tool.get("name"))
        table.add_row(name, "no" if reason else "yes", reason or _why_safe(tool, allow_destructive))
    out.print(table)
    out.print(
        f"{called} of {len(tools)} tool(s) would be called by CONF, FUZZ, ROB and SEC "
        "(FUZZ calls each of them --examples times).",
        highlight=False,
    )


def _why_safe(tool: dict[str, object], allow_destructive: bool) -> str:
    annotations = tool.get("annotations")
    hints = annotations if isinstance(annotations, dict) else {}
    if hints.get("readOnlyHint") is True:
        return "readOnlyHint: true"
    if hints.get("destructiveHint") is False:
        return "destructiveHint: false"
    return "--allow-destructive" if allow_destructive else ""


def _format_report(
    report: ReportFormat,
    result: RunResult,
    spec: ServerSpec,
    catalog: tuple[Check, ...],
    cases_dir: Path,
    sarif_path: str | None,
) -> str:
    from mcpprobe.engine.connection import StdioServer
    from mcpprobe.report.common import ReportContext

    if report is ReportFormat.JSON:
        from mcpprobe.report.json_report import dumps

        return dumps(result, spec)
    ctx = ReportContext.build(result, catalog, cases_dir.as_posix())
    if report is ReportFormat.JUNIT:
        from mcpprobe.report import junit

        return junit.dumps(ctx)
    if report is ReportFormat.SARIF:
        from mcpprobe.report import sarif

        argv = spec.argv if isinstance(spec, StdioServer) else None
        location = sarif_path or sarif.default_location(argv, Path.cwd())
        return sarif.dumps(ctx, location)
    from mcpprobe.report import html

    return html.dumps(ctx, spec)


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
