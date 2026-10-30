from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from mcpprobe.engine.catalog import Severity, Suite
from mcpprobe.engine.connection import HttpServer, Reply, StdioServer, encode
from mcpprobe.engine.replay import replay
from mcpprobe.engine.runner import (
    Case,
    CaseContext,
    Outcome,
    Runner,
    ServerUnavailable,
    Status,
    summarize,
)
from mcpprobe.exit_codes import ExitCode
from mcpprobe.transport.frames import Recording

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


def _call(method: str, mid: int = 1, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}


def echo_case(
    case_id: str,
    *,
    expect: object = None,
    suite: Suite = Suite.CONF,
    severity: Severity = Severity.ERROR,
) -> Case:
    async def run(ctx: CaseContext) -> Outcome:
        reply = await ctx.connection.call(_call("echo", v=expect), ctx.timeout)
        if reply.result == {"echo": {"v": expect}}:
            return Outcome.passed()
        return Outcome.failed(f"unexpected reply {reply.response}")

    return Case(case_id, suite, severity, run)


def custom_case(
    case_id: str,
    fn: Callable[[CaseContext], Awaitable[Outcome]],
    suite: Suite = Suite.CONF,
    severity: Severity = Severity.ERROR,
) -> Case:
    return Case(case_id, suite, severity, fn)


async def _crash(ctx: CaseContext) -> Outcome:
    await ctx.connection.call(_call("crash"), ctx.timeout)
    return Outcome.passed()


async def _hang(ctx: CaseContext) -> Outcome:
    await ctx.connection.call(_call("silent"), 60)
    return Outcome.passed()


async def _boom(ctx: CaseContext) -> Outcome:
    raise ValueError("bug in the check")


async def _fail(ctx: CaseContext) -> Outcome:
    await ctx.connection.call(_call("echo", x=1), ctx.timeout)
    return Outcome.failed("server did the wrong thing")


@pytest.fixture
def stdio_spec(stdio_argv: list[str]) -> StdioServer:
    return StdioServer(tuple(stdio_argv))


def _runner(spec: StdioServer | HttpServer, tmp_path: Path, **kwargs: object) -> Runner:
    options: dict[str, object] = {"case_timeout": 3.0, "liveness_timeout": 3.0, **kwargs}
    return Runner(spec, 42, tmp_path / "cases", **options)  # type: ignore[arg-type]


async def test_passing_run(stdio_spec: StdioServer, tmp_path: Path) -> None:
    result = await _runner(stdio_spec, tmp_path).run(
        [(Suite.CONF, [echo_case("CONF-002"), echo_case("CONF-001", expect=5)])]
    )
    assert [r.case_id for r in result.results] == ["CONF-001", "CONF-002"]
    assert all(r.status is Status.PASSED for r in result.results)
    assert result.exit_code is ExitCode.PASSED
    assert not (tmp_path / "cases").exists()
    assert summarize(result)["passed"] == 2


async def test_failure_is_saved_and_replays(stdio_spec: StdioServer, tmp_path: Path) -> None:
    result = await _runner(stdio_spec, tmp_path).run([(Suite.CONF, [custom_case("CONF-1", _fail)])])
    (failed,) = result.results
    assert failed.status is Status.FAILED
    assert result.exit_code is ExitCode.FAILED
    assert failed.repro is not None
    saved = Recording.loads((tmp_path / "cases" / failed.repro).read_text(encoding="utf-8"))
    replayed = await replay(saved, timeout=5)
    assert replayed.matched, replayed.differences


async def test_crash_is_attributed_and_server_restarted(
    stdio_spec: StdioServer, tmp_path: Path
) -> None:
    cases = [custom_case("CONF-A", _crash), echo_case("CONF-B"), echo_case("CONF-C")]
    result = await _runner(stdio_spec, tmp_path).run([(Suite.CONF, cases)])
    by_id = {r.case_id: r for r in result.results}
    assert by_id["CONF-A"].status is Status.CRASHED
    assert "stopped responding" in by_id["CONF-A"].message
    assert by_id["CONF-A"].repro is not None
    assert by_id["CONF-B"].status is Status.PASSED
    assert by_id["CONF-C"].status is Status.PASSED
    assert result.restarts == 1
    assert result.exit_code is ExitCode.FAILED


async def test_hang_times_out_and_run_continues(stdio_spec: StdioServer, tmp_path: Path) -> None:
    runner = _runner(stdio_spec, tmp_path, case_timeout=0.5)
    result = await runner.run([(Suite.CONF, [custom_case("CONF-A", _hang), echo_case("CONF-B")])])
    by_id = {r.case_id: r for r in result.results}
    assert by_id["CONF-A"].status is Status.TIMEOUT
    assert by_id["CONF-B"].status is Status.PASSED
    assert result.restarts == 0


async def test_bug_in_check_is_reported_as_error(stdio_spec: StdioServer, tmp_path: Path) -> None:
    result = await _runner(stdio_spec, tmp_path).run([(Suite.CONF, [custom_case("CONF-A", _boom)])])
    assert result.results[0].status is Status.ERROR
    assert "bug in the check" in result.results[0].message
    assert result.exit_code is ExitCode.PROBE_ERROR


async def test_warn_failure_does_not_fail_run(stdio_spec: StdioServer, tmp_path: Path) -> None:
    case = custom_case("CONF-A", _fail, severity=Severity.WARN)
    result = await _runner(stdio_spec, tmp_path).run([(Suite.CONF, [case])])
    assert result.results[0].status is Status.FAILED
    assert result.exit_code is ExitCode.PASSED


async def test_conformance_failure_stops_later_suites(
    stdio_spec: StdioServer, tmp_path: Path
) -> None:
    plan = [
        (Suite.CONF, [custom_case("CONF-A", _fail)]),
        (Suite.FUZZ, [echo_case("FUZZ-A", suite=Suite.FUZZ)]),
    ]
    stopped = await _runner(stdio_spec, tmp_path).run(plan)
    assert stopped.stopped_early
    assert {r.case_id: r.status for r in stopped.results}["FUZZ-A"] is Status.SKIPPED

    kept = await _runner(stdio_spec, tmp_path, keep_going=True).run(plan)
    assert not kept.stopped_early
    assert {r.case_id: r.status for r in kept.results}["FUZZ-A"] is Status.PASSED


async def test_case_order_is_seeded(stdio_spec: StdioServer, tmp_path: Path) -> None:
    def make(log: list[str]) -> list[Case]:
        def recorder(case_id: str) -> Callable[[CaseContext], Awaitable[Outcome]]:
            async def run(ctx: CaseContext) -> Outcome:
                log.append(ctx.case_id)
                return Outcome.passed()

            return run

        return [custom_case(f"CONF-{i:02d}", recorder(f"CONF-{i:02d}")) for i in range(8)]

    orders: dict[int, list[str]] = {}
    for seed in (1, 1, 2):
        log: list[str] = []
        runner = Runner(stdio_spec, seed, tmp_path / "cases")
        result = await runner.run([(Suite.CONF, make(log))])
        assert [r.case_id for r in result.results] == sorted(log)  # report is order-free
        if seed in orders:
            assert orders[seed] == log
        orders[seed] = log
    assert orders[1] != orders[2]


async def test_restart_limit_skips_remaining(stdio_spec: StdioServer, tmp_path: Path) -> None:
    cases = [custom_case("CONF-A", _crash), echo_case("CONF-B")]
    runner = _runner(stdio_spec, tmp_path, max_restarts=0)
    result = await runner.run([(Suite.CONF, cases)])
    by_id = {r.case_id: r for r in result.results}
    assert by_id["CONF-A"].status is Status.CRASHED
    assert by_id["CONF-B"].status is Status.SKIPPED


async def test_server_that_never_answers_is_unavailable(tmp_path: Path) -> None:
    silent = StdioServer((sys.executable, "-c", "import sys; sys.stdin.read()"))
    with pytest.raises(ServerUnavailable, match="did not answer"):
        await _runner(silent, tmp_path, liveness_timeout=0.5).run([])


async def test_server_that_cannot_start_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(ServerUnavailable, match="did not start"):
        await _runner(StdioServer(("no-such-program-xyz",)), tmp_path).run([])


async def test_server_that_exits_reports_stderr(tmp_path: Path) -> None:
    dies = StdioServer((sys.executable, "-c", "import sys; sys.stderr.write('missing API_KEY\\n')"))
    with pytest.raises(ServerUnavailable, match="missing API_KEY"):
        await _runner(dies, tmp_path).run([])


async def test_http_run(http_url: str, tmp_path: Path) -> None:
    async def tools_call(ctx: CaseContext) -> Outcome:
        reply = await ctx.connection.call(
            _call("tools/call", name="get weather ✓", arguments={}), ctx.timeout
        )
        header = reply.result["mcp_name_header"]
        return Outcome.passed() if header.startswith("=?base64?") else Outcome.failed(header)

    async def stream(ctx: CaseContext) -> Outcome:
        reply = await ctx.connection.call(_call("stream"), ctx.timeout)
        ok = reply.result == {"done": True} and len(reply.notifications) == 3
        return Outcome.passed() if ok else Outcome.failed(str(reply))

    async def notify(ctx: CaseContext) -> Outcome:
        reply = await ctx.connection.call({"jsonrpc": "2.0", "method": "note"}, ctx.timeout)
        return Outcome.passed() if reply.http_status == 202 else Outcome.failed(str(reply))

    cases = [
        custom_case("CONF-A", tools_call),
        custom_case("CONF-B", stream),
        custom_case("CONF-C", notify),
    ]
    result = await _runner(HttpServer(http_url), tmp_path).run([(Suite.CONF, cases)])
    assert [r.status for r in result.results] == [Status.PASSED] * 3, result.results


async def test_unreachable_http_server(tmp_path: Path) -> None:
    with pytest.raises(ServerUnavailable):
        await _runner(HttpServer("http://127.0.0.1:9/mcp"), tmp_path).run([])


def test_reply_accessors() -> None:
    reply = Reply({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "x"}})
    assert reply.result is None
    assert reply.error == {"code": -32601, "message": "x"}
    assert Reply(None).error is None
    assert encode(b"raw") == b"raw"
    assert encode({"a": "é"}) == '{"a":"é"}'.encode()
