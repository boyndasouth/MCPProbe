"""Conformance checks for the 2026-07-28 revision (milestone M1, first batch).

Each function implements one case. Its docstring quotes nothing; the YAML in
``checks/`` cites the exact spec section. Cases fail only on behavior the
spec rules out with a MUST (error checks) or SHOULD (warn checks); where the
spec allows more than one answer, every allowed answer passes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcpprobe.engine.catalog import Transport
from mcpprobe.engine.connection import HttpConnection, StdioConnection, encode
from mcpprobe.engine.runner import Outcome, Status
from mcpprobe.protocol.http_headers import request_metadata_headers
from mcpprobe.protocol.modern import (
    CLIENT_CAPABILITIES,
    CORE_RESULT_TYPES,
    HEADER_MISMATCH,
    INVALID_PARAMS,
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
    error_code,
    first_response,
    http_post,
    modern_call,
)
from mcpprobe.transport.frames import StdioFrame

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcpprobe.engine.connection import Reply
    from mcpprobe.engine.runner import CaseContext

ACCEPT = "application/json, text/event-stream"


def _has_tools(ctx: CaseContext) -> bool:
    return "tools" in ctx.facts.capabilities


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
    return await _unsupported_version(ctx, _ordinary_method(ctx), "conf-003")


async def conf_020(ctx: CaseContext) -> Outcome:
    """A valid request first, so a server that keeps version state is caught."""
    method = _ordinary_method(ctx)
    first = await modern_call(ctx, method, "conf-020-valid")
    if first.response is None:
        return Outcome.skipped(f"{method} did not answer a valid request")
    outcome = await _unsupported_version(ctx, method, "conf-020-unsupported")
    if outcome.status is Status.FAILED:
        return Outcome.failed(
            f"after a valid {VERSION} request, {outcome.message}; the server appears to keep "
            "the protocol version from earlier requests"
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
    message = request("server/discover", None)
    response, status = await first_response(ctx, message, min(QUIET_WINDOW, ctx.timeout))
    # Rejecting with an error, or not answering at all, both refuse the request.
    if response is not None and "result" in response:
        return Outcome.failed(f"a request with id null was accepted ({describe(response, status)})")
    return Outcome.passed()


async def conf_005_echo(ctx: CaseContext) -> Outcome:
    for request_id in ("conf-005:é/1", 9007199254740991):
        response, _ = await first_response(ctx, request("server/discover", request_id), ctx.timeout)
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
    body = encode(request("server/discover", "conf-007"))
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
        return Outcome.skipped("server does not declare the tools capability")
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
    await modern_call(ctx, "server/discover", "conf-015")
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
    ("CONF-020", "", conf_020, BOTH),
]

__all__ = ["CASES", "VERSION"]
