"""ROB: every check against its passing and failing fixture, on each transport."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mcpprobe.engine.catalog import Suite, Transport, load_catalog
from mcpprobe.engine.connection import HttpServer
from mcpprobe.engine.runner import Runner, Status
from mcpprobe.report.json_report import dumps
from mcpprobe.suites import SuiteOptions, collect
from mcpprobe.suites.rob.checks import CASES
from tests.support.reference import reference, stdio_spec

if TYPE_CHECKING:
    from pathlib import Path

    from mcpprobe.engine.connection import ServerSpec
    from mcpprobe.engine.runner import RunResult

ROB_IDS = sorted({check_id for check_id, _, _, _ in CASES})
# A break that only shows on one transport (HTTP has no cancellation notifications).
BREAK_TRANSPORTS = {"ROB-006": (Transport.STDIO,)}
# A break that is also, correctly, caught by another check.
ALSO_FAILS = {"ROB-007": {"ROB-003"}}


async def run_rob(
    spec: ServerSpec, transport: Transport, tmp_path: Path, seed: int = 42
) -> RunResult:
    plan = collect([Suite.ROB], load_catalog(), SuiteOptions(transport=transport))
    runner = Runner(spec, seed, tmp_path / "cases", case_timeout=5, liveness_timeout=5)
    return await runner.run(plan)


def _spec(
    transport: Transport, http_fixture: Any, breaks: tuple[str, ...] = (), era: str = "modern"
) -> ServerSpec:
    if transport is Transport.STDIO:
        return stdio_spec(breaks, era)
    return HttpServer(http_fixture(set(breaks), era).url)


def test_every_rob_check_has_a_catalog_entry_and_a_break() -> None:
    catalog = {check.id for check in load_catalog() if check.suite is Suite.ROB}
    assert set(ROB_IDS) == catalog
    for check_id in ROB_IDS:
        assert check_id in reference.BREAKS


@pytest.mark.parametrize("era", ["modern", "legacy"])
@pytest.mark.parametrize("transport", [Transport.STDIO, Transport.HTTP])
async def test_reference_server_passes_every_rob_check(
    transport: Transport, era: str, tmp_path: Path, http_fixture: Any
) -> None:
    result = await run_rob(_spec(transport, http_fixture, era=era), transport, tmp_path)
    assert {r.case_id.split("/")[0].split("@")[0] for r in result.results} == set(ROB_IDS)
    assert all(r.status is Status.PASSED for r in result.results), result.results
    assert result.restarts == 0
    suffix = "@legacy" if era == "legacy" else ""
    assert all(r.case_id.endswith(suffix) for r in result.results)


@pytest.mark.parametrize(
    ("check_id", "transport"),
    [
        (check_id, transport)
        for check_id in ROB_IDS
        for transport in BREAK_TRANSPORTS.get(check_id, (Transport.STDIO, Transport.HTTP))
    ],
)
async def test_each_check_fails_its_bad_fixture(
    check_id: str, transport: Transport, tmp_path: Path, http_fixture: Any
) -> None:
    result = await run_rob(_spec(transport, http_fixture, (check_id,)), transport, tmp_path)
    failed = {r.case_id.split("/")[0] for r in result.results if r.status is not Status.PASSED}
    assert check_id in failed, result.results
    assert failed <= {check_id} | ALSO_FAILS.get(check_id, set()), result.results
    for r in result.results:
        if r.status is Status.FAILED:
            assert r.repro is not None
            assert (tmp_path / "cases" / r.repro).is_file()


async def test_rob_runs_once_on_dual_era_servers(tmp_path: Path) -> None:
    result = await run_rob(stdio_spec(era="dual"), Transport.STDIO, tmp_path)
    assert result.results
    assert not any(r.case_id.endswith("@legacy") for r in result.results)
    assert all(r.status is Status.PASSED for r in result.results), result.results


async def test_same_seed_same_report(tmp_path: Path, http_fixture: Any) -> None:
    spec = _spec(Transport.HTTP, http_fixture, ("ROB-007",))
    first = await run_rob(spec, Transport.HTTP, tmp_path / "a", seed=3)
    second = await run_rob(spec, Transport.HTTP, tmp_path / "b", seed=3)
    assert dumps(first, spec) == dumps(second, spec)
