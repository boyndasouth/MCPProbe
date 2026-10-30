"""One live connection to the server under test, with a JSON-RPC ``call`` helper.

Suites get both levels: ``call`` for ordinary request/response exchanges,
and the raw transport (``.stdio`` or ``.http``) when a case must send
something malformed. Everything goes through the same recorder either way.

``call`` correlates by JSON-RPC ``id`` and collects any notifications that
arrive before the matching response. It adds no protocol semantics: a
JSON-RPC error is a perfectly good reply, not an exception.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcpprobe.protocol.http_headers import request_metadata_headers
from mcpprobe.transport.errors import TransportTimeout
from mcpprobe.transport.frames import HttpTarget, StdioTarget
from mcpprobe.transport.http import HttpTransport
from mcpprobe.transport.recorder import Masker, Recorder
from mcpprobe.transport.stdio import StdioTransport

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcpprobe.transport.frames import Header, Recording, Target

ACCEPT = "application/json, text/event-stream"
LEGACY_SESSION_HEADER = "Mcp-Session-Id"

JsonObject = dict[str, Any]


# ---------------------------------------------------------------- server specs


@dataclass(frozen=True)
class StdioServer:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True)
class HttpServer:
    url: str
    headers: tuple[Header, ...] = ()


ServerSpec = StdioServer | HttpServer


# ---------------------------------------------------------------------- reply


@dataclass(frozen=True)
class Reply:
    """What came back for one ``call``."""

    response: JsonObject | None
    """The JSON-RPC response with the matching id; None for notifications."""
    notifications: tuple[JsonObject, ...] = ()
    other: tuple[bytes, ...] = ()
    """Anything else that arrived meanwhile: unparsable lines, unrelated ids."""
    http_status: int | None = None

    @property
    def result(self) -> Any:
        return None if self.response is None else self.response.get("result")

    @property
    def error(self) -> JsonObject | None:
        error = None if self.response is None else self.response.get("error")
        return error if isinstance(error, dict) else None


def encode(message: JsonObject | bytes) -> bytes:
    """Compact, key-order-preserving JSON. Bytes pass through untouched."""
    if isinstance(message, bytes):
        return message
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _parse(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _message_id(message: JsonObject | bytes) -> tuple[bool, Any]:
    parsed = _parse(message) if isinstance(message, bytes) else message
    if isinstance(parsed, dict) and "id" in parsed and "method" in parsed:
        return True, parsed["id"]
    return False, None


# ----------------------------------------------------------------- connections


class StdioConnection:
    def __init__(self, spec: StdioServer, *, max_line_bytes: int | None = None) -> None:
        self.spec = spec
        self.recorder = Recorder()
        kwargs: dict[str, Any] = {}
        if max_line_bytes is not None:
            kwargs["max_line_bytes"] = max_line_bytes
        self.stdio = StdioTransport(
            spec.argv, recorder=self.recorder, env=spec.env, cwd=spec.cwd, **kwargs
        )

    async def start(self) -> None:
        await self.stdio.start()

    async def close(self) -> None:
        await self.stdio.close()

    @property
    def running(self) -> bool:
        return self.stdio.returncode is None

    def target(self) -> Target:
        return StdioTarget(
            argv=self.spec.argv, cwd=self.spec.cwd, env_keys=tuple(sorted(self.spec.env))
        )

    def masker(self) -> Masker:
        return Masker(self.spec.env.values())

    async def call(self, message: JsonObject | bytes, timeout: float) -> Reply:
        await self.stdio.send_line(encode(message))
        is_request, wanted = _message_id(message)
        if not is_request:
            return Reply(response=None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        notifications: list[JsonObject] = []
        other: list[bytes] = []
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TransportTimeout(f"no response with id {wanted!r} within {timeout:g}s")
            line = await self.stdio.receive(remaining)
            parsed = _parse(line)
            is_response = isinstance(parsed, dict) and "method" not in parsed and "id" in parsed
            if is_response and parsed["id"] == wanted:
                return Reply(parsed, tuple(notifications), tuple(other))
            if isinstance(parsed, dict) and "method" in parsed and "id" not in parsed:
                notifications.append(parsed)
            else:
                other.append(line)


class HttpConnection:
    def __init__(self, spec: HttpServer, *, timeout: float = 10.0) -> None:
        self.spec = spec
        self.recorder = Recorder()
        self.http = HttpTransport(
            spec.url, recorder=self.recorder, headers=spec.headers, timeout=timeout
        )
        self._session: str | None = None
        self._closed = False

    async def start(self) -> None:
        """HTTP servers are not ours to start; this only resets per-connection state."""
        self._session = None

    async def close(self) -> None:
        self._closed = True
        await self.http.aclose()

    @property
    def running(self) -> bool:
        return not self._closed

    def target(self) -> Target:
        return HttpTarget(url=self.spec.url)

    def masker(self) -> Masker:
        masker = Masker()
        masker.add_headers(self.spec.headers)
        return masker

    def conformant_headers(self, body: bytes) -> list[Header]:
        headers: list[Header] = [("Content-Type", "application/json"), ("Accept", ACCEPT)]
        headers.extend(request_metadata_headers(body))
        if self._session is not None:
            headers.append((LEGACY_SESSION_HEADER, self._session))
        return headers

    async def call(self, message: JsonObject | bytes, timeout: float) -> Reply:
        body = encode(message)
        exchange = await self.http.request(
            "POST", body, self.conformant_headers(body), timeout=timeout
        )
        self._session = exchange.header(LEGACY_SESSION_HEADER) or self._session
        _, wanted = _message_id(message)
        candidates: Sequence[bytes | str]
        if exchange.is_sse:
            candidates = [event.data for event in exchange.events]
        else:
            candidates = [exchange.body] if exchange.body else []
        response: JsonObject | None = None
        notifications: list[JsonObject] = []
        other: list[bytes] = []
        for item in candidates:
            parsed = _parse(item)
            if isinstance(parsed, dict) and "method" not in parsed and "id" in parsed:
                if response is None and parsed.get("id") == wanted:
                    response = parsed
                    continue
                # A modern error with no id (e.g. Origin rejected) still answers us.
                if response is None and parsed.get("id") is None and "error" in parsed:
                    response = parsed
                    continue
            if isinstance(parsed, dict) and "method" in parsed and "id" not in parsed:
                notifications.append(parsed)
            else:
                other.append(item if isinstance(item, bytes) else item.encode("utf-8"))
        return Reply(response, tuple(notifications), tuple(other), exchange.status)


Connection = StdioConnection | HttpConnection


def connect(spec: ServerSpec, *, timeout: float = 10.0) -> Connection:
    if isinstance(spec, StdioServer):
        return StdioConnection(spec)
    return HttpConnection(spec, timeout=timeout)


def recording_of(connection: Connection) -> Recording:
    """Everything this connection sent and received so far, secrets masked."""
    return connection.recorder.recording(connection.target(), connection.masker())
