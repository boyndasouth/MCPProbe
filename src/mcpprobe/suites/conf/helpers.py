"""Shared plumbing for conformance cases."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcpprobe.engine.connection import HttpConnection, StdioConnection, encode
from mcpprobe.protocol import legacy
from mcpprobe.protocol.modern import request
from mcpprobe.transport.errors import TransportTimeout

if TYPE_CHECKING:
    from mcpprobe.engine.connection import Reply
    from mcpprobe.engine.runner import CaseContext

JsonObject = dict[str, Any]
QUIET_WINDOW = 2.0
"""How long to wait when silence is an acceptable answer."""


def era_request(
    ctx: CaseContext, method: str, request_id: Any, params: JsonObject | None = None
) -> JsonObject:
    """A request in the case's era: modern ``_meta`` or plain legacy params."""
    if ctx.mode == "legacy":
        return legacy.request(method, request_id, params)
    return request(method, request_id, params)


async def era_call(
    ctx: CaseContext, method: str, request_id: str, params: JsonObject | None = None
) -> Reply:
    return await ctx.connection.call(era_request(ctx, method, request_id, params), ctx.timeout)


async def modern_call(
    ctx: CaseContext, method: str, request_id: str, params: JsonObject | None = None
) -> Reply:
    return await ctx.connection.call(request(method, request_id, params), ctx.timeout)


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


async def first_response(
    ctx: CaseContext, message: JsonObject, timeout: float
) -> tuple[JsonObject | None, int | None]:
    """Send ``message`` and return the first JSON-RPC response, whatever its id.

    Used where correlating by id is itself under test. Returns ``(None, None)``
    if nothing arrives in time on stdio.
    """
    connection = ctx.connection
    if isinstance(connection, StdioConnection):
        await connection.stdio.send_line(encode(message))
        try:
            while True:
                parsed = _parse(await connection.stdio.receive(timeout))
                if _is_response(parsed):
                    return parsed, None
        except TransportTimeout:
            return None, None
    assert isinstance(connection, HttpConnection)
    body = encode(message)
    return await http_post(ctx, body, connection.conformant_headers(body), timeout)


async def http_post(
    ctx: CaseContext, body: bytes, headers: list[tuple[str, str]], timeout: float | None = None
) -> tuple[JsonObject | None, int | None]:
    """POST exactly these headers; return (first response, HTTP status)."""
    connection = ctx.connection
    assert isinstance(connection, HttpConnection)
    exchange = await connection.http.request("POST", body, headers, timeout=timeout or ctx.timeout)
    items: list[bytes | str] = (
        [event.data for event in exchange.events] if exchange.is_sse else [exchange.body]
    )
    for item in items:
        parsed = _parse(item)
        if _is_response(parsed):
            return parsed, exchange.status
    return None, exchange.status


def error_code(response: JsonObject | None) -> Any:
    if response is None:
        return None
    error = response.get("error")
    return error.get("code") if isinstance(error, dict) else None


def describe(response: JsonObject | None, status: int | None = None) -> str:
    """Short, deterministic description of what came back, for messages."""
    prefix = f"HTTP {status}, " if status is not None else ""
    if response is None:
        return f"{prefix}no JSON-RPC response"
    if "error" in response and isinstance(response["error"], dict):
        error = response["error"]
        return f"{prefix}error {error.get('code')} ({error.get('message')!s:.80})"
    return f"{prefix}a result"
