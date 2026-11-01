"""Conformance checks for the 2026-07-28 revision (milestone M1, first batch).

Each function implements one case. Its docstring quotes nothing; the YAML in
``checks/`` cites the exact spec section. Cases fail only on behavior the
spec rules out with a MUST (error checks) or SHOULD (warn checks); where the
spec allows more than one answer, every allowed answer passes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from jsonschema.exceptions import SchemaError

from mcpprobe.engine.catalog import Transport
from mcpprobe.engine.connection import HttpConnection, StdioConnection, encode
from mcpprobe.engine.runner import Outcome, Status
from mcpprobe.protocol import legacy
from mcpprobe.protocol.era import Era
from mcpprobe.protocol.http_headers import request_metadata_headers
from mcpprobe.protocol.instances import validator_for
from mcpprobe.protocol.modern import (
    CLIENT_CAPABILITIES,
    CORE_RESULT_TYPES,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    PROTOCOL_VERSION,
    SERVER_INFO,
    UNSUPPORTED_PROTOCOL_VERSION,
    VERSION,
    meta,
    request,
)
from mcpprobe.suites.conf.helpers import (
    QUIET_WINDOW,
    describe,
    era_call,
    era_request,
    error_code,
    first_response,
    http_post,
    modern_call,
)
from mcpprobe.suites.conf.tools import (
    FULL_CAPABILITIES,
    INPUT_REQUEST_CAPABILITY,
    ToolInventory,
    call_tool,
    list_tools,
)
from mcpprobe.transport.errors import TransportTimeout
from mcpprobe.transport.frames import StdioFrame

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcpprobe.engine.connection import Connection, Reply
    from mcpprobe.engine.runner import CaseContext

ACCEPT = "application/json, text/event-stream"
JsonObject = dict[str, Any]


def _has_tools(ctx: CaseContext) -> bool:
    return "tools" in ctx.capabilities


def _probe_method(ctx: CaseContext) -> str:
    """A harmless request every server of the case's era must answer somehow."""
    return "ping" if ctx.mode == "legacy" else "server/discover"


def _no_tools(ctx: CaseContext) -> Outcome:
    """Skip reason for tool checks; honest about *why* tools are unavailable."""
    if not ctx.capabilities_known:
        source = "initialize" if ctx.mode == "legacy" else "server/discover"
        return Outcome.skipped(
            f"server capabilities are unknown because {source} did not return them"
        )
    return Outcome.skipped("server does not declare the tools capability")


async def _cacheable_results(ctx: CaseContext) -> list[tuple[str, Reply]]:
    """Results of the cacheable operations this server supports."""
    replies = [("server/discover", await modern_call(ctx, "server/discover", "conf-discover"))]
    if _has_tools(ctx):
        replies.append(("tools/list", await modern_call(ctx, "tools/list", "conf-tools-list")))
    return replies


# ------------------------------------------------------------------ CONF-001


async def conf_001(ctx: CaseContext) -> Outcome:
    reply = await modern_call(ctx, "server/discover", "conf-001")
    if reply.error is not None:
        return Outcome.failed(f"server/discover returned {describe(reply.response)}")
    result = reply.result
    if not isinstance(result, dict):
        return Outcome.failed("server/discover returned no result object")
    versions = result.get("supportedVersions")
    if not (isinstance(versions, list) and versions and all(isinstance(v, str) for v in versions)):
        return Outcome.failed("supportedVersions is missing or not a non-empty list of strings")
    if not isinstance(result.get("capabilities"), dict):
        return Outcome.failed("capabilities is missing or not an object")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-002


def conf_002(missing: str) -> Callable[[CaseContext], Awaitable[Outcome]]:
    async def run(ctx: CaseContext) -> Outcome:
        message = request(
            "server/discover",
            f"conf-002-{missing.rsplit('/', 1)[1]}",
            request_meta=meta(drop=(missing,)),
        )
        if isinstance(ctx.connection, HttpConnection):
            body = encode(message)
            response, status = await http_post(ctx, body, ctx.connection.conformant_headers(body))
        else:
            reply = await ctx.connection.call(message, ctx.timeout)
            response, status = reply.response, None
        if (
            ctx.facts.era is Era.DUAL
            and missing == PROTOCOL_VERSION
            and response is not None
            and isinstance(response.get("error"), dict)
        ):
            # Without protocolVersion a dual-era server cannot tell which era the
            # request belongs to; refusing it as a legacy request is defensible.
            return Outcome.passed()
        if error_code(response) != INVALID_PARAMS:
            return Outcome.failed(
                f"request without {missing} got {describe(response, status)}, expected "
                f"error {INVALID_PARAMS}"
            )
        if status is not None and status != 400:
            return Outcome.failed(f"expected HTTP 400 for error {INVALID_PARAMS}, got {status}")
        return Outcome.passed()

    return run


# ------------------------------------------------------------------ CONF-003


LIST_METHODS = (
    ("tools", "tools/list"),
    ("prompts", "prompts/list"),
    ("resources", "resources/list"),
)


def _ordinary_method(ctx: CaseContext) -> str:
    """A plain list method the server declares, else server/discover."""
    for capability, method in LIST_METHODS:
        if capability in ctx.facts.capabilities:
            return method
    return "server/discover"


async def conf_003(ctx: CaseContext) -> Outcome:
    """The unsupported version is the first request a fresh process sees (stdio).

    On the shared connection, earlier valid requests could make a server that
    remembers the version accept 1900-01-01; that is CONF-020's finding, and
    blaming it on version checking would be wrong. Over HTTP the probe cannot
    restart the server, so the shared connection is used.
    """
    if isinstance(ctx.connection, StdioConnection) and ctx.open_fresh is not None:
        fresh = await ctx.open_fresh()
        ctx = replace(ctx, connection=fresh, timeout=max(ctx.timeout, ctx.startup_timeout))
    return await _unsupported_version(ctx, _ordinary_method(ctx), "conf-003")


async def conf_020(ctx: CaseContext) -> Outcome:
    """Unsupported, valid, unsupported again, on a brand-new server process.

    The first request is the control: a server that accepts 1900-01-01 before
    it has seen anything else never checks the version at all. That is
    CONF-003's finding, so this case skips instead of reporting it twice. Only
    a server that refuses the version at first and accepts it after a valid
    request is remembering state between requests.
    """
    assert ctx.open_fresh is not None
    fresh = await ctx.open_fresh()
    method = _ordinary_method(ctx)
    probe = replace(ctx, connection=fresh, timeout=max(ctx.timeout, ctx.startup_timeout))
    control = await _unsupported_version(probe, method, "conf-020-control")
    if control.status is Status.FAILED:
        return Outcome.skipped(
            f"the server accepts version 1900-01-01 even as the first request, so it does not "
            f"check versions at all; see CONF-003 ({control.message})"
        )
    probe = replace(probe, timeout=ctx.timeout)
    first = await modern_call(probe, method, "conf-020-valid")
    if first.response is None:
        return Outcome.skipped(f"{method} did not answer a valid request")
    outcome = await _unsupported_version(probe, method, "conf-020-unsupported")
    if outcome.status is Status.FAILED:
        return Outcome.failed(
            f"version 1900-01-01 was refused as the first request, but after a valid {VERSION} "
            f"request {outcome.message}; the server keeps the protocol version from earlier "
            "requests"
        )
    return outcome


async def _unsupported_version(ctx: CaseContext, method: str, request_id: str) -> Outcome:
    requested = "1900-01-01"
    message = request(method, request_id, request_meta=meta(version=requested))
    if isinstance(ctx.connection, HttpConnection):
        body = encode(message)
        response, status = await http_post(ctx, body, ctx.connection.conformant_headers(body))
    else:
        reply = await ctx.connection.call(message, ctx.timeout)
        response, status = reply.response, None
    if error_code(response) != UNSUPPORTED_PROTOCOL_VERSION:
        return Outcome.failed(
            f"{method} with version {requested} got {describe(response, status)}, expected error "
            f"{UNSUPPORTED_PROTOCOL_VERSION}"
        )
    assert response is not None
    data = response["error"].get("data")
    data = data if isinstance(data, dict) else {}
    supported = data.get("supported")
    if not (
        isinstance(supported, list) and supported and all(isinstance(v, str) for v in supported)
    ):
        return Outcome.failed("error.data.supported is missing or not a non-empty list of strings")
    if data.get("requested") != requested:
        return Outcome.failed(f"error.data.requested should be {requested!r}")
    if status is not None and status != 400:
        return Outcome.failed(f"expected HTTP 400, got {status}")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-004


async def conf_004(ctx: CaseContext) -> Outcome:
    allow_extensions = bool(ctx.facts.capabilities.get("extensions"))
    for method, reply in await _cacheable_results(ctx):
        result = reply.result
        if not isinstance(result, dict):
            continue  # an error answer has no result to check
        value = result.get("resultType")
        if not isinstance(value, str):
            return Outcome.failed(f"{method} result has no resultType")
        if value not in CORE_RESULT_TYPES and not allow_extensions:
            return Outcome.failed(
                f"{method} resultType {value!r} is not a core value and no extensions "
                "are advertised"
            )
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-005


async def conf_005_null_id(ctx: CaseContext) -> Outcome:
    message = era_request(ctx, _probe_method(ctx), None)
    response, status = await first_response(ctx, message, min(QUIET_WINDOW, ctx.timeout))
    # Rejecting with an error, or not answering at all, both refuse the request.
    if response is not None and "result" in response:
        return Outcome.failed(f"a request with id null was accepted ({describe(response, status)})")
    return Outcome.passed()


async def conf_005_echo(ctx: CaseContext) -> Outcome:
    for request_id in ("conf-005:é/1", 9007199254740991):
        response, _ = await first_response(
            ctx, era_request(ctx, _probe_method(ctx), request_id), ctx.timeout
        )
        if response is None:
            return Outcome.failed(f"no response to a request with id {request_id!r}")
        got = response.get("id")
        if type(got) is not type(request_id) or got != request_id:
            return Outcome.failed(f"request id {request_id!r} came back as {got!r}")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-006


def conf_006(variant: str) -> Callable[[CaseContext], Awaitable[Outcome]]:
    async def run(ctx: CaseContext) -> Outcome:
        body = encode(request("server/discover", f"conf-006-{variant}"))
        headers = dict(request_metadata_headers(body))
        if variant == "method":
            headers["Mcp-Method"] = "tools/list"
        elif variant == "version":
            headers["MCP-Protocol-Version"] = "2025-11-25"
        else:  # missing
            del headers["Mcp-Method"]
        sent = [("Content-Type", "application/json"), ("Accept", ACCEPT), *headers.items()]
        response, status = await http_post(ctx, body, sent)
        if variant == "version" and ctx.facts.era is Era.DUAL and status == 400:
            # The header names a legacy revision. The spec does not say whether a
            # dual-era server must pick the era from the body or may route by the
            # header; either way the request is refused, which is what matters.
            return Outcome.passed()
        if status != 400 or error_code(response) != HEADER_MISMATCH:
            return Outcome.failed(
                f"{_variant_text(variant)} got {describe(response, status)}, expected HTTP 400 "
                f"with error {HEADER_MISMATCH}"
            )
        return Outcome.passed()

    return run


def _variant_text(variant: str) -> str:
    return {
        "method": "Mcp-Method header that disagrees with the body",
        "version": "MCP-Protocol-Version header that disagrees with the body",
        "missing": "request without the Mcp-Method header",
    }[variant]


# ------------------------------------------------------------------ CONF-007


async def conf_007(ctx: CaseContext) -> Outcome:
    body = encode(era_request(ctx, _probe_method(ctx), "conf-007"))
    connection = ctx.connection
    assert isinstance(connection, HttpConnection)
    headers = [*connection.conformant_headers(body), ("Origin", "https://mcpprobe-rebind.invalid")]
    response, status = await http_post(ctx, body, headers)
    if status != 403:
        return Outcome.failed(
            f"a request from a foreign Origin got {describe(response, status)}, expected 403"
        )
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-012


async def conf_012(ctx: CaseContext) -> Outcome:
    if not _has_tools(ctx):
        return _no_tools(ctx)
    orders: list[list[Any]] = []
    for attempt in range(3):
        reply = await modern_call(ctx, "tools/list", f"conf-012-{attempt}")
        result = reply.result
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return Outcome.skipped(f"tools/list returned {describe(reply.response)}")
        orders.append([t.get("name") for t in result["tools"] if isinstance(t, dict)])
    if any(order != orders[0] for order in orders[1:]):
        return Outcome.failed("tools/list returned the same tools in a different order")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-015


async def conf_015(ctx: CaseContext) -> Outcome:
    connection = ctx.connection
    assert isinstance(connection, StdioConnection)
    await era_call(ctx, _probe_method(ctx), "conf-015")
    for frame in connection.recorder.frames:
        if not (isinstance(frame, StdioFrame) and frame.stream == "stdout"):
            continue
        line = frame.data
        preview = line[:80].decode("utf-8", "replace").rstrip()
        if not line.endswith(b"\n"):
            return Outcome.failed(f"stdout ended mid-line: {preview!r}")
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return Outcome.failed(f"stdout carried a line that is not JSON: {preview!r}")
        if not (isinstance(message, dict) and message.get("jsonrpc") == "2.0"):
            return Outcome.failed(f"stdout carried a non-JSON-RPC line: {preview!r}")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-016


async def conf_016(ctx: CaseContext) -> Outcome:
    if not ctx.facts.capabilities_known:
        return Outcome.skipped("server/discover did not return capabilities")
    if "logging" in ctx.facts.capabilities:
        return Outcome.failed("declares the logging capability, deprecated in 2026-07-28")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-017


async def conf_017(ctx: CaseContext) -> Outcome:
    for method, reply in await _cacheable_results(ctx):
        result = reply.result
        if not isinstance(result, dict):
            continue
        result_meta = result.get("_meta")
        info = result_meta.get(SERVER_INFO) if isinstance(result_meta, dict) else None
        if not (isinstance(info, dict) and isinstance(info.get("name"), str)):
            return Outcome.failed(f"{method} result has no _meta {SERVER_INFO}")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-018


async def conf_018(ctx: CaseContext) -> Outcome:
    for method, reply in await _cacheable_results(ctx):
        result = reply.result
        if not isinstance(result, dict) or result.get("resultType", "complete") != "complete":
            continue
        ttl = result.get("ttlMs")
        if not (isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 0):
            return Outcome.failed(f"{method} result has no ttlMs integer >= 0")
        if result.get("cacheScope") not in ("public", "private"):
            return Outcome.failed(f"{method} result has no cacheScope of public or private")
    return Outcome.passed()


# ------------------------------------------------------------------ CONF-008

UNKNOWN_TOOL = "mcpprobe-no-such-tool"


async def conf_008(ctx: CaseContext) -> Outcome:
    """Calling a tool that does not exist is a protocol error, not a tool result."""
    if not _has_tools(ctx):
        return _no_tools(ctx)
    inventory = await list_tools(ctx, allow_destructive=True)
    names = {tool.name for tool in inventory.callable} | {name for name, _ in inventory.skipped}
    name = UNKNOWN_TOOL
    while name in names:  # never call a real tool by accident
        name += "-x"
    reply = await era_call(ctx, "tools/call", "conf-008", {"name": name, "arguments": {}})
    if reply.error is not None:
        return Outcome.passed()
    result = reply.result
    if isinstance(result, dict) and result.get("isError") is True:
        return Outcome.failed(
            f"calling unknown tool {name!r} returned a tool result with isError: true; "
            "the spec lists an unknown tool as a protocol error (JSON-RPC error)"
        )
    return Outcome.failed(f"calling unknown tool {name!r} returned {describe(reply.response)}")


# ------------------------------------------------- CONF-009 / 010 / 011 (tools)

ALLOWED_INPUT_METHODS = frozenset(INPUT_REQUEST_CAPABILITY)
NON_MRTR_METHODS = ("server/discover", "tools/list")


def _skip_note(inventory: ToolInventory) -> str:
    if not inventory.skipped:
        return ""
    names = ", ".join(name for name, _ in inventory.skipped[:5])
    more = f" and {len(inventory.skipped) - 5} more" if len(inventory.skipped) > 5 else ""
    count = len(inventory.skipped)
    return f"; {count} tool(s) not called for safety or lack of arguments ({names}{more})"


async def conf_009(ctx: CaseContext) -> Outcome:
    """Call tools declaring no client capabilities; undeclared input requests are a violation."""
    if not _has_tools(ctx):
        return _no_tools(ctx)
    inventory = await list_tools(ctx, ctx.allow_destructive)
    exercised = 0
    for tool in inventory.callable:
        exchange = await call_tool(ctx, tool, capabilities={}, follow=False)
        reply = exchange.final
        if reply is None:
            continue
        result = reply.result
        if isinstance(result, dict) and result.get("resultType") == "input_required":
            requests = result.get("inputRequests")
            for key, item in requests.items() if isinstance(requests, dict) else []:
                method = item.get("method") if isinstance(item, dict) else None
                capability = INPUT_REQUEST_CAPABILITY.get(str(method))
                if capability is not None:
                    return Outcome.failed(
                        f"tool {tool.name} sent a {method} input request ({key!r}) although the "
                        f"client declared no {capability} capability"
                    )
            continue
        error = reply.error
        if error is not None and error.get("code") == MISSING_REQUIRED_CLIENT_CAPABILITY:
            exercised += 1
            data = error.get("data")
            required = data.get("requiredCapabilities") if isinstance(data, dict) else None
            if not isinstance(required, dict) or not required:
                return Outcome.failed(
                    f"tool {tool.name} returned -32021 without data.requiredCapabilities"
                )
            if reply.http_status is not None and reply.http_status != 400:
                return Outcome.failed(
                    f"tool {tool.name}: -32021 came with HTTP {reply.http_status}"
                )
    if not exercised:
        return Outcome.skipped(
            "no callable tool needed a client capability" + _skip_note(inventory)
        )
    return Outcome.passed()


def _input_required_problem(result: JsonObject) -> str | None:
    requests = result.get("inputRequests")
    state = result.get("requestState")
    if requests is None and state is None:
        return "carries neither inputRequests nor requestState"
    if state is not None and not isinstance(state, str):
        return "has a requestState that is not a string"
    if requests is not None:
        if not isinstance(requests, dict):
            return "has inputRequests that is not an object"
        for key, item in requests.items():
            method = item.get("method") if isinstance(item, dict) else None
            if method not in ALLOWED_INPUT_METHODS:
                return f"has input request {key!r} with method {method!r}, which is not allowed"
    return None


async def conf_010(ctx: CaseContext) -> Outcome:
    """input_required results are well formed, and only appear where allowed."""
    for method in NON_MRTR_METHODS:
        if method == "tools/list" and not _has_tools(ctx):
            continue
        reply = await modern_call(ctx, method, f"conf-010-{method}")
        result = reply.result
        if isinstance(result, dict) and result.get("resultType") == "input_required":
            return Outcome.failed(
                f"{method} returned input_required, which only prompts/get, "
                "resources/read and tools/call may do"
            )
    if not _has_tools(ctx):
        return _no_tools(ctx)
    inventory = await list_tools(ctx, ctx.allow_destructive)
    rounds = 0
    for tool in inventory.callable:
        exchange = await call_tool(ctx, tool, FULL_CAPABILITIES)
        for reply in exchange.replies:
            result = reply.result
            if not (isinstance(result, dict) and result.get("resultType") == "input_required"):
                continue
            rounds += 1
            problem = _input_required_problem(result)
            if problem is not None:
                return Outcome.failed(f"tool {tool.name}: input_required result {problem}")
    if not rounds:
        return Outcome.skipped("no callable tool asked for input" + _skip_note(inventory))
    return Outcome.passed()


async def conf_011(ctx: CaseContext) -> Outcome:
    """structuredContent is present and valid wherever a tool declares outputSchema."""
    if not _has_tools(ctx):
        return _no_tools(ctx)
    inventory = await list_tools(ctx, ctx.allow_destructive)
    checked = 0
    for tool in inventory.callable:
        schema = tool.definition.get("outputSchema")
        if not isinstance(schema, dict):
            continue
        exchange = await call_tool(ctx, tool, FULL_CAPABILITIES)
        result = exchange.final.result if exchange.final else None
        if not isinstance(result, dict) or result.get("resultType", "complete") != "complete":
            continue
        if result.get("isError") is True:
            continue  # a tool execution error carries no structured result
        checked += 1
        if "structuredContent" not in result:
            return Outcome.failed(
                f"tool {tool.name} declares outputSchema but returned no structuredContent"
            )
        try:
            errors = sorted(
                validator_for(schema).iter_errors(result["structuredContent"]),
                key=lambda e: list(e.path),
            )
        except SchemaError as exc:
            return Outcome.failed(f"tool {tool.name} has an invalid outputSchema: {exc.message}")
        if errors:
            first = errors[0]
            where = "/".join(str(p) for p in first.path) or "(root)"
            return Outcome.failed(
                f"tool {tool.name}: structuredContent does not match outputSchema at {where}: "
                f"{first.message}"
            )
    if not checked:
        return Outcome.skipped(
            "no callable tool with an outputSchema returned a result" + _skip_note(inventory)
        )
    return Outcome.passed()


# --------------------------------------------- CONF-013 / 014 / 021 / 022 (eras)

UNSUPPORTED_LEGACY_VERSION = "1900-01-01"
VISIBLE_ASCII = frozenset(range(0x21, 0x7F))


async def _fresh_initialize(
    ctx: CaseContext, request_id: str, version: str
) -> tuple[Connection, Reply | None]:
    """initialize on a brand-new connection (a legacy session is single-use)."""
    assert ctx.open_fresh is not None
    fresh = await ctx.open_fresh()
    try:
        reply = await fresh.call(legacy.initialize(request_id, version), ctx.startup_timeout)
    except TransportTimeout:
        reply = None
    return fresh, reply


async def _initialized_problem(ctx: CaseContext, fresh: Connection) -> str | None:
    """Send notifications/initialized; the server must accept it without a reply."""
    body = encode(legacy.INITIALIZED)
    if isinstance(fresh, HttpConnection):
        exchange = await fresh.http.request("POST", body, fresh.conformant_headers(body))
        if exchange.status != 202:
            return f"notifications/initialized got HTTP {exchange.status}, expected 202 Accepted"
        return None
    assert isinstance(fresh, StdioConnection)
    await fresh.stdio.send_line(body)
    try:
        while True:
            line = await fresh.stdio.receive(min(QUIET_WINDOW, ctx.timeout))
            try:
                message = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # CONF-015 reports non-JSON output
            if isinstance(message, dict) and "method" not in message:
                return "the server responded to notifications/initialized, a notification"
    except TransportTimeout:
        return None


async def conf_013_handshake(ctx: CaseContext) -> Outcome:
    fresh, reply = await _fresh_initialize(ctx, "conf-013-initialize", legacy.LATEST)
    if reply is None or reply.response is None:
        return Outcome.failed(f"no response to initialize within {ctx.startup_timeout:g}s")
    result = reply.result
    if not isinstance(result, dict):
        return Outcome.failed(f"initialize returned {describe(reply.response, reply.http_status)}")
    version = result.get("protocolVersion")
    if not isinstance(version, str):
        return Outcome.failed("InitializeResult has no protocolVersion string")
    if not isinstance(result.get("capabilities"), dict):
        return Outcome.failed("InitializeResult has no capabilities object")
    info = result.get("serverInfo")
    if not (isinstance(info, dict) and isinstance(info.get("name"), str)):
        return Outcome.failed("InitializeResult has no serverInfo with a name")
    session = fresh.session_id if isinstance(fresh, HttpConnection) else None
    if session is not None and (not session or any(ord(c) not in VISIBLE_ASCII for c in session)):
        return Outcome.failed("Mcp-Session-Id contains characters outside visible ASCII")
    fresh.legacy_version = version
    problem = await _initialized_problem(ctx, fresh)
    if problem is not None:
        return Outcome.failed(problem)
    method = "tools/list" if "tools" in result["capabilities"] else "ping"
    after = await fresh.call(legacy.request(method, "conf-013-operation"), ctx.timeout)
    if after.error is not None or after.result is None:
        return Outcome.failed(
            f"after a completed handshake, {method} returned "
            f"{describe(after.response, after.http_status)}"
        )
    return Outcome.passed()


async def conf_013_negotiation(ctx: CaseContext) -> Outcome:
    _, reply = await _fresh_initialize(ctx, "conf-013-version", UNSUPPORTED_LEGACY_VERSION)
    if reply is None or reply.response is None:
        return Outcome.failed(
            f"no response to initialize with version {UNSUPPORTED_LEGACY_VERSION}"
        )
    if reply.error is not None:
        return Outcome.passed()  # the spec's own example; CONF-021 notes it
    offered = legacy.negotiated_version(reply)
    if offered is None:
        return Outcome.failed("InitializeResult has no protocolVersion string")
    if offered == UNSUPPORTED_LEGACY_VERSION:
        return Outcome.failed(
            f"the server agreed to version {UNSUPPORTED_LEGACY_VERSION}, which it cannot "
            "support; it must answer with a version it supports"
        )
    return Outcome.passed()


async def conf_021(ctx: CaseContext) -> Outcome:
    _, reply = await _fresh_initialize(ctx, "conf-021", UNSUPPORTED_LEGACY_VERSION)
    if reply is None or reply.response is None:
        return Outcome.skipped("initialize got no response (reported by CONF-013)")
    if reply.error is not None:
        return Outcome.failed(
            f"initialize with an unsupported version got {describe(reply.response)} instead of "
            "a result offering a version the server supports"
        )
    return Outcome.passed()


async def conf_022(ctx: CaseContext) -> Outcome:
    version = ctx.facts.legacy_version
    if version in legacy.OUTDATED:
        return Outcome.failed(
            f"asked for {legacy.LATEST}, the server negotiated {version}; consider upgrading to "
            f"{legacy.LATEST} or 2026-07-28"
        )
    return Outcome.passed()


async def conf_014(ctx: CaseContext) -> Outcome:
    versions = (ctx.facts.discover or {}).get("supportedVersions")
    listed = [v for v in versions if v in legacy.VERSIONS] if isinstance(versions, list) else []
    if not listed:
        return Outcome.skipped("supportedVersions lists no initialize-era revision")
    for version in sorted(listed):
        _, reply = await _fresh_initialize(ctx, f"conf-014-{version}", version)
        got = legacy.negotiated_version(reply)
        if got != version:
            answer = "no response" if reply is None else describe(reply.response, reply.http_status)
            if got is not None:
                answer = f"version {got}"
            return Outcome.failed(
                f"server/discover lists {version} in supportedVersions, but initialize with "
                f"{version} got {answer}"
            )
    return Outcome.passed()


# ------------------------------------------------------------------- registry

# (check id, case suffix, implementation, transports it applies to)
CaseSpec = tuple[str, str, "Callable[[CaseContext], Awaitable[Outcome]]", frozenset[Transport]]
BOTH = frozenset({Transport.STDIO, Transport.HTTP})
STDIO = frozenset({Transport.STDIO})
HTTP = frozenset({Transport.HTTP})

CASES: list[CaseSpec] = [
    ("CONF-001", "", conf_001, BOTH),
    # On HTTP a body without protocolVersion also mismatches the required
    # header, so -32020 is equally correct there; only stdio is unambiguous.
    ("CONF-002", "protocolVersion", conf_002(PROTOCOL_VERSION), STDIO),
    ("CONF-002", "clientCapabilities", conf_002(CLIENT_CAPABILITIES), BOTH),
    ("CONF-003", "", conf_003, BOTH),
    ("CONF-004", "", conf_004, BOTH),
    ("CONF-005", "null-id", conf_005_null_id, BOTH),
    ("CONF-005", "echo", conf_005_echo, BOTH),
    ("CONF-006", "method", conf_006("method"), HTTP),
    ("CONF-006", "version", conf_006("version"), HTTP),
    ("CONF-006", "missing", conf_006("missing"), HTTP),
    ("CONF-007", "", conf_007, HTTP),
    ("CONF-012", "", conf_012, BOTH),
    ("CONF-015", "", conf_015, STDIO),
    ("CONF-016", "", conf_016, BOTH),
    ("CONF-017", "", conf_017, BOTH),
    ("CONF-018", "", conf_018, BOTH),
    # stdio only: over HTTP the probe cannot restart the server, so it cannot
    # send a first request to a server that has seen nothing; CONF-003 covers
    # any HTTP server that accepts an unsupported version.
    ("CONF-020", "", conf_020, STDIO),
    ("CONF-008", "", conf_008, BOTH),
    ("CONF-009", "", conf_009, BOTH),
    ("CONF-010", "", conf_010, BOTH),
    ("CONF-011", "", conf_011, BOTH),
    ("CONF-013", "handshake", conf_013_handshake, BOTH),
    ("CONF-013", "negotiation", conf_013_negotiation, BOTH),
    ("CONF-014", "", conf_014, BOTH),
    ("CONF-021", "", conf_021, BOTH),
    ("CONF-022", "", conf_022, BOTH),
]

# Cases that open their own connections: each one may pay a server start-up.
FRESH_SESSIONS = {
    "CONF-003": 1,
    "CONF-013": 1,
    "CONF-020": 1,
    "CONF-014": len(legacy.VERSIONS),
    "CONF-021": 1,
}

# Cases that call every safe tool get this multiple of the per-case timeout.
TIMEOUT_FACTORS = {"CONF-009": 6.0, "CONF-010": 6.0, "CONF-011": 6.0}

__all__ = ["CASES", "FRESH_SESSIONS", "TIMEOUT_FACTORS", "VERSION"]
