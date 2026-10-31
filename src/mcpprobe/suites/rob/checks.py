"""ROB checks: malformed input, big messages, cancellation and concurrency.

Every ROB case runs once per server (in the modern era when the server speaks
it), and the runner's liveness probe after each case turns any crash or hang
into a finding, whatever the check's own severity. The checks themselves
judge the answer:

- ROB-001 (warn): malformed JSON gets a -32700 Parse error.
- ROB-002 (warn): invalid JSON-RPC envelopes are rejected, not executed.
- ROB-003 (error): unknown methods get -32601 (and HTTP 404 in the modern era).
- ROB-004 (warn): tools/call with arguments that are not an object is rejected
  without an internal error.
- ROB-005 (error): 1 MB and 10 MB requests are answered or rejected, never
  dropped.
- ROB-006 (error): cancellation notifications are handled gracefully: never
  answered, and the server keeps working.
- ROB-007 (error): concurrent requests (``--concurrency``, default 8) each get
  their own response.

Request bodies are sent byte for byte through the wire client, so the saved
case replays exactly what was sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from mcpprobe.engine.catalog import Transport
from mcpprobe.engine.connection import HttpConnection, StdioConnection, encode
from mcpprobe.engine.runner import Outcome
from mcpprobe.protocol.modern import INVALID_PARAMS, METHOD_NOT_FOUND, PARSE_ERROR
from mcpprobe.suites.conf.helpers import QUIET_WINDOW, era_request
from mcpprobe.suites.conf.tools import list_tools
from mcpprobe.transport.errors import TransportClosed, TransportTimeout

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcpprobe.engine.runner import CaseContext

JsonObject = dict[str, Any]
INTERNAL_ERROR: Final = -32603
UNKNOWN_METHOD: Final = "mcpprobe/no-such-method"
PAYLOAD_SIZES: Final = {"1mb": 1_000_000, "10mb": 10_000_000}
BATCHING_VERSION: Final = "2025-03-26"
PADDING_KEY: Final = "com.mcpprobe/padding"
"""A vendor-prefixed ``_meta`` key: servers must ignore what they don't know."""


# ------------------------------------------------------------------ plumbing


@dataclass(frozen=True)
class Answer:
    """What came back for one raw message."""

    responses: tuple[JsonObject, ...] = ()
    """JSON-RPC responses, in arrival order (any id)."""
    status: int | None = None
    """HTTP status; None on stdio."""
    dropped: bool = False
    """HTTP only: the connection closed without any response."""

    @property
    def first(self) -> JsonObject | None:
        return self.responses[0] if self.responses else None

    def describe(self) -> str:
        prefix = f"HTTP {self.status}, " if self.status is not None else ""
        if self.dropped:
            return "the connection was closed without a response"
        response = self.first
        if response is None:
            return f"{prefix}no JSON-RPC response"
        error = response.get("error")
        if isinstance(error, dict):
            message = " ".join(str(error.get("message", "")).split())[:80]
            return f"{prefix}error {error.get('code')} ({message})"
        return f"{prefix}a result"


def _parse(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _is_response(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and "method" not in value
        and ("result" in value or "error" in value)
    )


def _code(response: JsonObject | None) -> Any:
    error = None if response is None else response.get("error")
    return error.get("code") if isinstance(error, dict) else None


async def _collect_stdio(
    connection: StdioConnection, wanted: int, timeout: float
) -> list[JsonObject]:
    """Responses read from stdout until ``wanted`` arrived or ``timeout`` passed."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    responses: list[JsonObject] = []
    while len(responses) < wanted:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            parsed = _parse(await connection.stdio.receive(remaining))
        except TransportTimeout:
            break
        if _is_response(parsed):
            responses.append(parsed)
    return responses


async def send_raw(ctx: CaseContext, body: bytes, timeout: float) -> Answer:
    """Send ``body`` exactly; wait up to ``timeout`` for the first response."""
    connection = ctx.connection
    if isinstance(connection, StdioConnection):
        await connection.stdio.send_line(body)
        return Answer(tuple(await _collect_stdio(connection, 1, timeout)))
    assert isinstance(connection, HttpConnection)
    try:
        exchange = await connection.http.request(
            "POST", body, connection.conformant_headers(body), timeout=timeout
        )
    except TransportClosed:
        return Answer(dropped=True)
    items: list[bytes | str] = (
        [event.data for event in exchange.events] if exchange.is_sse else [exchange.body]
    )
    responses = tuple(p for p in (_parse(i) for i in items) if _is_response(p))
    return Answer(responses, exchange.status)


def _rejected(answer: Answer) -> bool:
    """A JSON-RPC error, or on HTTP a 4xx status (with or without an error body)."""
    if answer.dropped:
        return False
    if answer.first is not None:
        return "error" in answer.first
    return answer.status is not None and 400 <= answer.status < 500


# ------------------------------------------------------------------ ROB-001


async def rob_001(ctx: CaseContext) -> Outcome:
    body = b'{"jsonrpc":"2.0","id":"rob-001","method":"tools/list","params":{'
    answer = await send_raw(ctx, body, min(QUIET_WINDOW, ctx.timeout))
    if _code(answer.first) == PARSE_ERROR:
        return Outcome.passed()
    if answer.first is None and answer.status is not None and 400 <= answer.status < 500:
        return Outcome.passed()  # an HTTP-level rejection of a body that is not JSON
    return Outcome.failed(
        f"malformed JSON got {answer.describe()}, expected error {PARSE_ERROR} (Parse error)"
    )


# ------------------------------------------------------------------ ROB-002

ENVELOPES: Final[dict[str, Callable[[CaseContext], Any]]] = {
    "no-jsonrpc": lambda ctx: _without(era_request(ctx, "tools/list", "rob-002"), "jsonrpc"),
    "jsonrpc-1.0": lambda ctx: {**era_request(ctx, "tools/list", "rob-002"), "jsonrpc": "1.0"},
    "method-not-string": lambda ctx: {**era_request(ctx, "tools/list", "rob-002"), "method": 42},
    "batch": lambda ctx: [era_request(ctx, "tools/list", "rob-002")],
}
_ENVELOPE_TEXT: Final = {
    "no-jsonrpc": 'a request without "jsonrpc"',
    "jsonrpc-1.0": 'a request with "jsonrpc": "1.0"',
    "method-not-string": "a request whose method is a number",
    "batch": "a JSON array holding one request (batching is not part of MCP)",
}


def _without(message: JsonObject, key: str) -> JsonObject:
    return {k: v for k, v in message.items() if k != key}


async def rob_002(ctx: CaseContext) -> Outcome:
    """All four envelopes in one case: one finding per server, not one per variant."""
    failures: list[str] = []
    variants = list(ENVELOPES)
    if ctx.connection.legacy_version == BATCHING_VERSION:
        variants.remove("batch")  # 2025-03-26 had JSON-RPC batching; 2025-06-18 removed it
    for variant in variants:
        body = encode(ENVELOPES[variant](ctx)) if variant != "batch" else _batch(ctx)
        answer = await send_raw(ctx, body, min(QUIET_WINDOW, ctx.timeout))
        if not _rejected(answer):
            failures.append(f"{_ENVELOPE_TEXT[variant]} got {answer.describe()}")
    if not failures:
        return Outcome.passed()
    more = f" (and {len(failures) - 1} more of {len(variants)})" if len(failures) > 1 else ""
    return Outcome.failed(f"{failures[0]}{more}; expected an error (-32600 Invalid Request)")


def _batch(ctx: CaseContext) -> bytes:
    return json.dumps(ENVELOPES["batch"](ctx), separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------ ROB-003


async def rob_003(ctx: CaseContext) -> Outcome:
    answer = await send_raw(ctx, encode(era_request(ctx, UNKNOWN_METHOD, "rob-003")), ctx.timeout)
    response = answer.first
    if _code(response) != METHOD_NOT_FOUND:
        return Outcome.failed(
            f"unknown method {UNKNOWN_METHOD} got {answer.describe()}, expected error "
            f"{METHOD_NOT_FOUND} (Method not found)"
        )
    if response is not None and response.get("id") != "rob-003":
        return Outcome.failed(f"the -32601 error came back with id {response.get('id')!r}")
    modern_http = answer.status is not None and ctx.mode == "modern"
    if modern_http and answer.status != 404:
        return Outcome.failed(
            f"unknown method got HTTP {answer.status} with -32601; 2026-07-28 Streamable "
            "HTTP requires 404 Not Found"
        )
    return Outcome.passed()


# ------------------------------------------------------------------ ROB-004


async def rob_004(ctx: CaseContext) -> Outcome:
    inventory = await list_tools(ctx, ctx.allow_destructive)
    if not inventory.callable:
        return Outcome.skipped("no tool can be called under the safety rules")
    # Prefer a read-only tool: the request should never reach the tool, but if
    # the server is lax enough to run it, nothing should change.
    read_only = [
        t for t in inventory.callable if (t.definition.get("annotations") or {}).get("readOnlyHint")
    ]
    tool = (read_only or list(inventory.callable))[0].name
    shapes: tuple[tuple[str, Any], ...] = (("an array", []), ("a string", "mcpprobe"))
    for label, arguments in shapes:
        params: JsonObject = {"name": tool, "arguments": arguments}
        body = encode(era_request(ctx, "tools/call", f"rob-004-{label[2:]}", params))
        answer = await send_raw(ctx, body, ctx.timeout)
        response = answer.first
        accepted = (
            response is not None
            and isinstance(response.get("result"), dict)
            and response["result"].get("isError") is not True
        )
        if (
            accepted
            or _code(response) == INTERNAL_ERROR
            or (response is None and not _rejected(answer))
        ):
            return Outcome.failed(
                f"tools/call {tool} with arguments that are {label} got {answer.describe()}, "
                f"expected a JSON-RPC error such as {INVALID_PARAMS} (Invalid params)"
            )
    return Outcome.passed()


# ------------------------------------------------------------------ ROB-005


def rob_005(size: str) -> Callable[[CaseContext], Awaitable[Outcome]]:
    async def run(ctx: CaseContext) -> Outcome:
        message = era_request(ctx, "tools/list", f"rob-005-{size}")
        params = message["params"] = dict(message.get("params") or {})
        params["_meta"] = {**params.get("_meta", {}), PADDING_KEY: ""}
        overhead = len(encode(message))
        params["_meta"][PADDING_KEY] = "x" * max(0, PAYLOAD_SIZES[size] - overhead)
        body = encode(message)
        answer = await send_raw(ctx, body, ctx.timeout)
        if answer.first is not None or _rejected(answer):
            return Outcome.passed()
        return Outcome.failed(
            f"a {len(body):,}-byte tools/list request got {answer.describe()}; it should be "
            "answered, or rejected with an error (HTTP 413 is fine)"
        )

    return run


# ------------------------------------------------------------------ ROB-006


def _cancelled(params: JsonObject) -> bytes:
    return encode({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params})


async def rob_006_stdio(ctx: CaseContext) -> Outcome:
    connection = ctx.connection
    assert isinstance(connection, StdioConnection)
    await connection.stdio.send_line(encode(era_request(ctx, "tools/list", "rob-006-a")))
    await connection.stdio.send_line(
        _cancelled({"requestId": "rob-006-a", "reason": "mcpprobe: cancelled at once"})
    )
    await connection.stdio.send_line(_cancelled({"requestId": "rob-006-never-sent"}))
    await connection.stdio.send_line(_cancelled({}))  # malformed: SHOULD be ignored
    await connection.stdio.send_line(encode(era_request(ctx, "tools/list", "rob-006-b")))
    seen: list[JsonObject] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.timeout
    while not any(r.get("id") == "rob-006-b" for r in seen):
        remaining = deadline - loop.time()
        batch = await _collect_stdio(connection, 1, remaining) if remaining > 0 else []
        if not batch:
            return Outcome.failed(
                "after a request was cancelled the server did not answer the next request"
            )
        seen.extend(batch)
    # Give a late answer to one of the notifications a moment to show up.
    seen.extend(await _collect_stdio(connection, 1, min(0.5, ctx.timeout)))
    stray = [r for r in seen if r.get("id") not in ("rob-006-a", "rob-006-b")]
    if stray:
        return Outcome.failed(
            "a notifications/cancelled message was answered (notifications MUST NOT get a "
            f"response): {_short(stray[0])}"
        )
    return Outcome.passed()


async def rob_006_http(ctx: CaseContext) -> Outcome:
    connection = ctx.connection
    assert isinstance(connection, HttpConnection)
    body = encode(era_request(ctx, "tools/list", "rob-006-a"))
    # Closing the response stream is the cancellation signal on Streamable HTTP;
    # stop reading after the first chunk (a JSON body may already be complete).
    with contextlib.suppress(TransportClosed):
        await connection.http.request(
            "POST",
            body,
            connection.conformant_headers(body),
            stop_after_events=0,
            timeout=ctx.timeout,
        )
    answer = await send_raw(ctx, encode(era_request(ctx, "tools/list", "rob-006-b")), ctx.timeout)
    if answer.first is None or answer.first.get("id") != "rob-006-b":
        return Outcome.failed(
            f"after the client disconnected mid-request, the next request got {answer.describe()}"
        )
    return Outcome.passed()


def _short(response: JsonObject) -> str:
    text = json.dumps(response, separators=(",", ":"), ensure_ascii=True)
    return text if len(text) <= 160 else text[:159] + "…"


# ------------------------------------------------------------------ ROB-007


def _concurrent_ids(ctx: CaseContext) -> list[tuple[Any, str]]:
    """(id, method) pairs: half tools/list, half an unknown method, ids of both
    JSON types, in an order taken from the run seed."""
    pairs: list[tuple[Any, str]] = []
    for n in range(ctx.concurrency):
        request_id: Any = f"rob-007-{n}" if n % 2 else 7000 + n
        pairs.append((request_id, "tools/list" if n % 4 < 2 else UNKNOWN_METHOD))
    ctx.rng.shuffle(pairs)
    return pairs


def _matches(method: str, response: JsonObject) -> bool:
    if method == "tools/list":
        result = response.get("result")
        return isinstance(result, dict) and isinstance(result.get("tools"), list)
    return "error" in response


async def rob_007(ctx: CaseContext) -> Outcome:
    pairs = _concurrent_ids(ctx)
    bodies = [encode(era_request(ctx, method, request_id)) for request_id, method in pairs]
    connection = ctx.connection
    responses: list[JsonObject] = []
    if isinstance(connection, StdioConnection):
        for body in bodies:
            await connection.stdio.send_line(body)
        responses = await _collect_stdio(connection, len(pairs), ctx.timeout)
    else:
        answers = await asyncio.gather(*(send_raw(ctx, body, ctx.timeout) for body in bodies))
        for answer in answers:
            responses.extend(answer.responses)
    expected = {_key(request_id): method for request_id, method in pairs}
    answered: dict[Any, int] = {}
    for response in responses:
        key = _key(response.get("id"))
        if key not in expected:
            return Outcome.failed(
                f"a response carried an id that was never sent: {_short(response)}"
            )
        answered[key] = answered.get(key, 0) + 1
        if not _matches(expected[key], response):
            return Outcome.failed(
                f"the response to request {response.get('id')!r} ({expected[key]}) answers a "
                f"different request: {_short(response)}"
            )
    missing = [request_id for request_id, _ in pairs if _key(request_id) not in answered]
    twice = sorted((str(k[1]) for k, n in answered.items() if n > 1))
    if missing:
        return Outcome.failed(
            f"{len(missing)} of {len(pairs)} concurrent requests got no response "
            f"(first: {missing[0]!r})"
        )
    if twice:
        return Outcome.failed(f"request {twice[0]} was answered more than once")
    return Outcome.passed()


def _key(request_id: Any) -> tuple[str, Any]:
    """Ids compare by JSON type as well as value: 7 and "7" are different ids."""
    return (type(request_id).__name__, request_id)


# --------------------------------------------------------------------- cases

CaseSpec = tuple[str, str, "Callable[[CaseContext], Awaitable[Outcome]]", frozenset[Transport]]
BOTH = frozenset({Transport.STDIO, Transport.HTTP})
STDIO = frozenset({Transport.STDIO})
HTTP = frozenset({Transport.HTTP})

CASES: list[CaseSpec] = [
    ("ROB-001", "", rob_001, BOTH),
    ("ROB-002", "", rob_002, BOTH),
    ("ROB-003", "", rob_003, BOTH),
    ("ROB-004", "", rob_004, BOTH),
    *[("ROB-005", size, rob_005(size), BOTH) for size in PAYLOAD_SIZES],
    ("ROB-006", "stdio", rob_006_stdio, STDIO),
    ("ROB-006", "disconnect", rob_006_http, HTTP),
    ("ROB-007", "", rob_007, BOTH),
]
TIMEOUT_FACTORS: Final = {"ROB-005": 3.0, "ROB-007": 2.0}
