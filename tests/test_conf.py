"""Every CONF check against its passing and failing fixture.

The reference server (fixtures/servers/reference.py) passes every check;
``--break <ID>`` makes it violate exactly that one. Each check must pass the
former and fail the latter on every transport it applies to.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from mcpprobe.engine.catalog import Suite, Transport, load_catalog
from mcpprobe.engine.connection import HttpServer, StdioServer
from mcpprobe.engine.runner import Runner, Status
from mcpprobe.exit_codes import ExitCode
from mcpprobe.protocol.era import Era
from mcpprobe.report.json_report import dumps
from mcpprobe.suites import SuiteOptions, collect
from mcpprobe.suites.conf.checks import CASES

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mcpprobe.engine.connection import ServerSpec
    from mcpprobe.engine.runner import RunResult

REFERENCE = Path(__file__).parents[1] / "fixtures" / "servers" / "reference.py"


def _load_reference() -> Any:
    spec = importlib.util.spec_from_file_location("mcpprobe_reference_fixture", REFERENCE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reference = _load_reference()


class HttpFixture:
    def __init__(self, breaks: set[str]) -> None:
        self.httpd = reference.make_http_server(reference.Server(breaks))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def stdio_spec(breaks: tuple[str, ...] = ()) -> StdioServer:
    flags = [arg for check in breaks for arg in ("--break", check)]
    return StdioServer((sys.executable, "-u", str(REFERENCE), *flags))


async def run_checks(
    spec: ServerSpec, transport: Transport, tmp_path: Path, only: str | None = None
) -> RunResult:
    catalog = load_catalog()
    plan = collect([Suite.CONF], catalog, SuiteOptions(transport=transport))
    if only is not None:
        plan = [(suite, [c for c in cases if c.id.split("/")[0] == only]) for suite, cases in plan]
    runner = Runner(spec, 42, tmp_path / "cases", case_timeout=5, liveness_timeout=5)
    return await runner.run(plan)


@pytest.fixture
def http_fixture() -> Iterator[Any]:
    fixtures: list[HttpFixture] = []

    def start(breaks: set[str] | None = None) -> HttpFixture:
        fixture = HttpFixture(breaks or set())
        fixtures.append(fixture)
        return fixture

    yield start
    for fixture in fixtures:
        fixture.close()


def _applicable() -> list[tuple[str, Transport]]:
    pairs = {(check_id, t) for check_id, _, _, transports in CASES for t in transports}
    return sorted(pairs)


def test_every_implemented_check_has_a_catalog_entry_and_a_break() -> None:
    ids = {check.id for check in load_catalog()}
    for check_id, _, _, _ in CASES:
        assert check_id in ids, check_id
        assert check_id in reference.BREAKS, f"{check_id} has no failing fixture"


@pytest.mark.parametrize(("check_id", "transport"), _applicable())
async def test_check_passes_good_and_fails_bad_fixture(
    check_id: str, transport: Transport, tmp_path: Path, http_fixture: Any
) -> None:
    def spec(breaks: tuple[str, ...]) -> ServerSpec:
        if transport is Transport.STDIO:
            return stdio_spec(breaks)
        return HttpServer(http_fixture(set(breaks)).url)

    good = await run_checks(spec(()), transport, tmp_path / "good", only=check_id)
    assert good.results, "no cases ran"
    assert all(r.status is Status.PASSED for r in good.results), good.results

    bad = await run_checks(spec((check_id,)), transport, tmp_path / "bad", only=check_id)
    statuses = {r.case_id: r.status for r in bad.results}
    assert Status.FAILED in statuses.values(), statuses
    assert all(s in (Status.PASSED, Status.FAILED) for s in statuses.values()), statuses


@pytest.mark.parametrize("transport", [Transport.STDIO, Transport.HTTP])
async def test_reference_server_passes_whole_suite(
    transport: Transport, tmp_path: Path, http_fixture: Any
) -> None:
    spec: ServerSpec = (
        stdio_spec() if transport is Transport.STDIO else HttpServer(http_fixture().url)
    )
    result = await run_checks(spec, transport, tmp_path)
    assert result.facts is not None
    assert result.facts.era is Era.MODERN
    assert [r for r in result.results if r.status is not Status.PASSED] == []
    assert result.exit_code is ExitCode.PASSED


async def test_json_report_is_byte_identical(tmp_path: Path) -> None:
    spec = stdio_spec(("CONF-004", "CONF-016"))
    first = dumps(await run_checks(spec, Transport.STDIO, tmp_path / "a"), spec)
    second = dumps(await run_checks(spec, Transport.STDIO, tmp_path / "b"), spec)
    assert first == second
    assert '"status": "failed"' in first


async def test_saved_case_replays(tmp_path: Path) -> None:
    from mcpprobe.engine.replay import replay
    from mcpprobe.transport.frames import Recording

    result = await run_checks(stdio_spec(("CONF-003",)), Transport.STDIO, tmp_path, "CONF-003")
    (failed,) = result.results
    assert failed.repro == "CONF-003.json"
    saved = Recording.loads((tmp_path / "cases" / failed.repro).read_text(encoding="utf-8"))
    replayed = await replay(saved, timeout=5)
    assert replayed.matched, replayed.differences


async def test_non_modern_server_skips_modern_checks(tmp_path: Path, stdio_argv: list[str]) -> None:
    result = await run_checks(StdioServer(tuple(stdio_argv)), Transport.STDIO, tmp_path)
    assert result.facts is not None
    assert result.facts.era is Era.NOT_MODERN
    assert result.results
    assert all(r.status is Status.SKIPPED for r in result.results)
    assert result.exit_code is ExitCode.PASSED


def test_reference_cli_lists_breaks(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        reference.main(["--help"])
    assert "--break" in capsys.readouterr().out


async def test_dual_era_server_is_not_pushed_into_legacy_mode(tmp_path: Path) -> None:
    sticky = Path(__file__).parent / "support" / "sticky_server.py"
    spec = StdioServer((sys.executable, "-u", str(sticky)))
    result = await run_checks(spec, Transport.STDIO, tmp_path, only="CONF-001")
    assert result.facts is not None
    assert result.facts.era is Era.MODERN
    assert [r.status for r in result.results] == [Status.PASSED]
