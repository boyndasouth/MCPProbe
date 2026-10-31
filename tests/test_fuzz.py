"""FUZZ: strategies, the search driver, and every check against its fixtures."""

from __future__ import annotations

import contextlib
import json
import random
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import find
from hypothesis.errors import NoSuchExample

from mcpprobe.engine.catalog import Suite, Transport, load_catalog
from mcpprobe.engine.connection import HttpServer, Reply, StdioServer
from mcpprobe.engine.runner import Runner, Status
from mcpprobe.protocol.instances import is_valid
from mcpprobe.report.json_report import dumps
from mcpprobe.suites import SuiteOptions, collect
from mcpprobe.suites.conf.tools import unsafe_reason
from mcpprobe.suites.fuzz.checks import CHECKS
from mcpprobe.suites.fuzz.engine import classify, show
from mcpprobe.suites.fuzz.strategies import (
    UnsupportedSchema,
    generator_schema,
    search_settings,
    tool_strategy,
)
from tests.support.reference import reference, stdio_spec

if TYPE_CHECKING:
    from mcpprobe.engine.connection import ServerSpec
    from mcpprobe.engine.runner import RunResult
    from mcpprobe.suites.fuzz.strategies import Example

SUPPORT = Path(__file__).parent / "support"
FUZZ_IDS = ("FUZZ-001", "FUZZ-002", "FUZZ-003", "FUZZ-004")


def _examples(schema: dict[str, Any], count: int = 60, seed: int = 1) -> list[Example]:
    strategy = tool_strategy(schema)
    seen: list[Example] = []

    def record(example: Example) -> bool:
        seen.append(example)
        return False

    with contextlib.suppress(NoSuchExample):
        find(strategy.examples, record, settings=search_settings(count), random=random.Random(seed))
    return seen


async def run_fuzz(
    spec: ServerSpec,
    tmp_path: Path,
    *,
    transport: Transport = Transport.STDIO,
    seed: int = 42,
    examples: int = 50,
    timeout: float = 5,
    liveness_timeout: float = 5,
    **runner_args: Any,
) -> RunResult:
    plan = collect([Suite.FUZZ], load_catalog(), SuiteOptions(transport=transport))
    runner = Runner(
        spec,
        seed,
        tmp_path / "cases",
        case_timeout=timeout,
        liveness_timeout=liveness_timeout,
        fuzz_examples=examples,
        **runner_args,
    )
    return await runner.run(plan)


def _by_id(result: RunResult) -> dict[str, Any]:
    return {r.case_id: r for r in result.results}


# ------------------------------------------------------------- strategies


def test_2020_12_schemas_are_translated_for_the_generator() -> None:
    schema = {
        "type": "object",
        "properties": {
            "pair": {"type": "array", "prefixItems": [{"type": "integer"}], "items": False},
            "ref": {"$ref": "#/$defs/name"},
        },
        "dependentRequired": {"pair": ["ref"]},
        "unevaluatedProperties": False,
        "$defs": {"name": {"type": "string", "minLength": 2}},
    }
    working = generator_schema(schema)
    assert working["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert working["properties"]["pair"]["items"] == [{"type": "integer"}]
    assert working["properties"]["pair"]["additionalItems"] is False
    assert working["properties"]["ref"] == {"$ref": "#/definitions/name"}
    assert working["dependencies"] == {"pair": ["ref"]}
    assert "unevaluatedProperties" not in working
    # Plain object schemas: undeclared properties are left out of valid values.
    plain = generator_schema({"type": "object", "properties": {"a": {"type": "string"}}})
    assert plain["additionalProperties"] is False
    assert "additionalProperties" not in working  # not plain: it has dependencies
    # Draft-07 schemas are left in their dialect.
    draft7 = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}
    assert generator_schema(draft7)["$schema"] == draft7["$schema"]


def test_generated_values_are_classified_by_the_real_validator() -> None:
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 0, "maximum": 9},
            "s": {"type": "string"},
        },
        "required": ["n"],
    }
    examples = _examples(schema)
    assert len(examples) == 60
    strategy = tool_strategy(schema)
    for example in examples:
        assert strategy.is_valid(example.arguments) == is_valid(schema, example.arguments)
        if example.mutation is None:
            assert is_valid(schema, example.arguments), example
    kinds = {e.mutation.kind for e in examples if e.mutation is not None}
    assert {"drop_required", "boundary", "wrong_type"} <= kinds
    assert any(not strategy.is_valid(e.arguments) for e in examples)


def test_generation_is_deterministic_per_seed() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    assert _examples(schema, seed=5) == _examples(schema, seed=5)
    assert _examples(schema, seed=5) != _examples(schema, seed=6)


def test_addresses_never_point_at_real_hosts() -> None:
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "mail": {"type": "string", "format": "email"},
            "host": {"type": "string", "format": "hostname"},
            "ip": {"type": "string", "format": "ipv4"},
        },
        "required": ["url", "mail", "host", "ip"],
    }
    for example in _examples(schema, count=40):
        if example.mutation is not None:
            continue
        args = example.arguments
        assert args["url"].endswith(".mcpprobe.invalid/")
        assert args["mail"].endswith("@mcpprobe.invalid")
        assert args["host"].endswith(".mcpprobe.invalid")
        assert args["ip"].startswith("192.0.2.")


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ({"type": "object", "properties": {"x": {"$ref": "https://e.example/s"}}}, "remote \\$ref"),
        ({"type": "object", "properties": {"x": {"$ref": "#"}}}, "generator"),
        ({"type": "object", "minProperties": "many"}, "not a valid JSON Schema"),
        ("not a schema", "not a JSON object"),
    ],
)
def test_unsupported_schemas_are_refused_not_guessed(schema: Any, reason: str) -> None:
    with pytest.raises(UnsupportedSchema, match=reason):
        tool_strategy(schema)


# ----------------------------------------------------------------- engine


def test_classify_and_show() -> None:
    def reply(body: dict[str, Any], status: int | None = None) -> Reply:
        return Reply({"jsonrpc": "2.0", "id": 1, **body} if body else None, http_status=status)

    assert classify(reply({"result": {"content": []}})).kind == "result"
    assert classify(reply({"result": {"isError": True}})).kind == "tool_error"
    assert classify(reply({"result": {"resultType": "input_required"}})).kind == "input_required"
    error = classify(reply({"error": {"code": -32603, "message": "boom"}}))
    assert (error.kind, error.code, error.detail) == ("rpc_error", -32603, "boom")
    assert classify(reply({}, 502)).describe() == "no JSON-RPC response (HTTP 502)"

    assert show({"s": "A" * 100_000}) == '{"s": "AAAAAAAAAAAA\\u2026(100000 chars)"}'
    assert show({"s": "\ud800"}) == '{"s": "\\ud800"}'
    deep: Any = "x"
    for _ in range(30):
        deep = [deep]
    assert "more levels" in show({"d": deep})


def test_safety_rules() -> None:
    open_world = {"annotations": {"readOnlyHint": True, "openWorldHint": True}}
    assert "allow-open-world" in str(
        unsafe_reason(open_world, allow_destructive=True, allow_open_world=False)
    )
    assert unsafe_reason(open_world, allow_destructive=False, allow_open_world=True) is None
    unannotated: dict[str, Any] = {}
    assert unsafe_reason(unannotated, allow_destructive=False, allow_open_world=True) is not None
    assert unsafe_reason(unannotated, allow_destructive=True, allow_open_world=False) is None


def test_every_fuzz_check_has_a_catalog_entry_and_a_break() -> None:
    ids = {check.id for check in load_catalog() if check.suite is Suite.FUZZ}
    assert ids == set(CHECKS) == set(FUZZ_IDS)
    for check_id in FUZZ_IDS:
        assert check_id in reference.BREAKS


# ------------------------------------------------------------- fixtures

FUZZED_TOOLS = ("ask_name", "echo", "get_time", "weather")


@pytest.mark.parametrize("transport", [Transport.STDIO, Transport.HTTP])
async def test_reference_server_passes_every_fuzz_check(
    transport: Transport, tmp_path: Path, http_fixture: Any
) -> None:
    spec: ServerSpec = (
        stdio_spec() if transport is Transport.STDIO else HttpServer(http_fixture().url)
    )
    result = await run_fuzz(spec, tmp_path, transport=transport)
    statuses = _by_id(result)
    for check_id in FUZZ_IDS:
        for tool in FUZZED_TOOLS:
            assert statuses[f"{check_id}/{tool}"].status is Status.PASSED, statuses
        # The trap tool is never called: it would shut the server down.
        trap = statuses[f"{check_id}/delete_everything"]
        assert trap.status is Status.SKIPPED
        assert "destructive" in trap.message
    assert result.restarts == 0


# check -> (tool, test for the shrunk arguments). Shrinking is exact for a given
# seed, but the expectations say what "minimal" means rather than pin one value.
SHRUNK: dict[str, tuple[str, Any]] = {
    "FUZZ-001": ("echo", lambda a: len(a["message"]) == 1 and not a["message"].isascii()),
    "FUZZ-002": ("echo", lambda a: a == {}),
    "FUZZ-003": ("echo", lambda a: list(a) == ["message"] and a["message"] in (None, 0, False)),
    "FUZZ-004": ("echo", lambda a: a == {"message": ""}),
}


@pytest.mark.parametrize("check_id", FUZZ_IDS)
@pytest.mark.parametrize("transport", [Transport.STDIO, Transport.HTTP])
async def test_each_check_fails_its_bad_fixture_with_a_minimal_repro(
    check_id: str, transport: Transport, tmp_path: Path, http_fixture: Any
) -> None:
    spec: ServerSpec = (
        stdio_spec((check_id,))
        if transport is Transport.STDIO
        else HttpServer(http_fixture({check_id}).url)
    )
    result = await run_fuzz(spec, tmp_path, transport=transport)
    tool, is_minimal = SHRUNK[check_id]
    finding = _by_id(result)[f"{check_id}/{tool}"]
    assert finding.status in (Status.FAILED, Status.CRASHED), finding
    assert "(not shrunk)" not in finding.message
    assert "did not reproduce" not in finding.message
    assert finding.repro is not None

    # The saved case holds only the readiness probe and the shrunk call.
    recording = json.loads((tmp_path / "cases" / finding.repro).read_text(encoding="utf-8"))
    sent = [_sent(frame) for frame in recording["frames"]]
    calls = [m for m in sent if m and m.get("method") == "tools/call"]
    assert len(calls) == 1, calls
    assert is_minimal(calls[0]["params"]["arguments"]), calls

    # No other check is violated by the break.
    others = [
        r
        for r in result.results
        if r.status is not Status.PASSED and r.status is not Status.SKIPPED
    ]
    assert {r.case_id.split("/")[0] for r in others} == {check_id}, others


def _sent(frame: dict[str, Any]) -> dict[str, Any] | None:
    if frame.get("stream") == "stdin":
        text = frame["data"]["utf8"]
    elif frame.get("kind") == "http_request":
        text = frame.get("body", {}).get("utf8", "")
    else:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


async def test_fuzz_runs_once_on_dual_era_servers(tmp_path: Path) -> None:
    result = await run_fuzz(stdio_spec(era="dual"), tmp_path)
    assert result.results
    assert not any(r.case_id.endswith("@legacy") for r in result.results)


async def test_fuzz_runs_in_legacy_sessions_on_legacy_servers(tmp_path: Path) -> None:
    result = await run_fuzz(stdio_spec(("FUZZ-004",), era="legacy"), tmp_path)
    finding = _by_id(result)["FUZZ-004/echo@legacy"]
    assert finding.status is Status.FAILED
    assert '{"message": ""}' in finding.message


async def test_same_seed_gives_identical_report_and_cases(tmp_path: Path) -> None:
    spec = stdio_spec(("FUZZ-001", "FUZZ-002"))
    first = await run_fuzz(spec, tmp_path / "a", seed=7)
    second = await run_fuzz(spec, tmp_path / "b", seed=7)
    assert dumps(first, spec) == dumps(second, spec)
    names = sorted(p.name for p in (tmp_path / "a" / "cases").iterdir())
    assert names
    for name in names:
        a = (tmp_path / "a" / "cases" / name).read_bytes()
        assert a == (tmp_path / "b" / "cases" / name).read_bytes(), name


async def test_hangs_are_reported_and_not_shrunk(tmp_path: Path) -> None:
    spec = StdioServer((sys.executable, "-u", str(SUPPORT / "tool_server.py"), "hang"))
    result = await run_fuzz(spec, tmp_path, timeout=1, liveness_timeout=1, examples=10)
    finding = _by_id(result)["FUZZ-001/slow"]
    assert finding.status in (Status.TIMEOUT, Status.CRASHED), finding
    assert "hung" in finding.message
    assert "(not shrunk)" in finding.message


async def test_dropped_requests_are_failures_not_hangs(tmp_path: Path) -> None:
    spec = StdioServer((sys.executable, "-u", str(SUPPORT / "tool_server.py"), "drop"))
    result = await run_fuzz(spec, tmp_path, timeout=1, examples=10)
    finding = _by_id(result)["FUZZ-001/slow"]
    assert finding.status is Status.FAILED, finding
    assert "the request was dropped" in finding.message
    assert result.restarts == 0  # the server was fine all along


async def test_progress_keeps_a_slow_tool_alive(tmp_path: Path) -> None:
    spec = StdioServer((sys.executable, "-u", str(SUPPORT / "tool_server.py"), "progress"))
    # 2.5 s per call against a 1 s timeout: only progress notifications save it.
    result = await run_fuzz(spec, tmp_path, timeout=1, examples=2)
    assert _by_id(result)["FUZZ-001/slow"].status is Status.PASSED


async def test_a_tool_still_reporting_progress_is_slow_not_stuck(tmp_path: Path) -> None:
    spec = StdioServer((sys.executable, "-u", str(SUPPORT / "tool_server.py"), "progress"))
    # 2.5 s per call with progress every 0.5 s, but at most 3 x 0.8 s in total:
    # it runs out of time while still reporting progress.
    result = await run_fuzz(spec, tmp_path, timeout=0.8, examples=2)
    finding = _by_id(result)["FUZZ-001/slow"]
    assert finding.status is Status.PASSED, finding
    assert "still reporting progress" in finding.message


async def test_open_world_and_unannotated_tools_need_opt_in(tmp_path: Path) -> None:
    spec = StdioServer((sys.executable, "-u", str(SUPPORT / "tool_server.py"), "annotated"))
    default = _by_id(await run_fuzz(spec, tmp_path / "a", examples=5))
    assert default["FUZZ-001/web"].status is Status.SKIPPED
    assert "openWorldHint" in default["FUZZ-001/web"].message
    assert default["FUZZ-002/web"].message == default["FUZZ-001/web"].message
    assert default["FUZZ-001/plain"].status is Status.SKIPPED

    opted = _by_id(
        await run_fuzz(
            spec, tmp_path / "b", examples=5, allow_open_world=True, allow_destructive=True
        )
    )
    assert opted["FUZZ-001/web"].status is Status.PASSED
    assert opted["FUZZ-001/plain"].status is Status.PASSED


async def test_unsupported_schema_is_skipped_with_the_reason(tmp_path: Path) -> None:
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"type": "object", "properties": {"x": {"$ref": "#"}}}))
    spec = StdioServer(
        (sys.executable, "-u", str(SUPPORT / "tool_server.py"), "schema", str(schema_file))
    )
    finding = _by_id(await run_fuzz(spec, tmp_path, examples=5))["FUZZ-001/probe"]
    assert finding.status is Status.SKIPPED
    assert finding.message.startswith("tool not fuzzed:")
