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
    from mcpprobe.transport.sse import SseEvent

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


PROGRESS = "notifications/progress"


def encode(message: JsonObject | bytes) -> bytes:
    """Compact, key-order-preserving JSON. Bytes pass through untouched."""
    if isinstance(message, bytes):
        return message
    text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate (fuzzed "odd Unicode") has no UTF-8 form; JSON can
        # still carry it as a \ud800 escape, which is what we want to send.
        return json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _parse(data: bytes | str) -> Any:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _progress_token(message: JsonObject | bytes) -> Any:
    if isinstance(message, bytes):
        return None
    params = message.get("params")
    request_meta = params.get("_meta") if isinstance(params, dict) else None
    return request_meta.get("progressToken") if isinstance(request_meta, dict) else None


def _is_progress_for(token: Any, item: Any) -> bool:
    """Whether ``item`` is a progress notification for ``token`` (never for no token)."""
    if token is None or not isinstance(item, dict) or item.get("method") != PROGRESS:
        return False
    params = item.get("params")
    return isinstance(params, dict) and params.get("progressToken") == token


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
        self.legacy_version: str | None = None
        """Set once a legacy initialize handshake succeeded on this connection."""
        self.extra_secrets: set[str] = set()
        """Secrets the server revealed (SEC-003), masked in saved recordings too."""

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
        return Masker([*self.spec.env.values(), *self.extra_secrets])

    async def call(
        self, message: JsonObject | bytes, timeout: float, *, max_timeout: float | None = None
    ) -> Reply:
        """Send ``message``; for a request, wait for the response with its id.

        With ``max_timeout``, each progress notification for the request's
        ``progressToken`` restarts the timeout, up to ``max_timeout`` in total
        (basic/patterns/cancellation#timeouts).
        """
        await self.stdio.send_line(encode(message))
        is_request, wanted = _message_id(message)
        if not is_request:
            return Reply(response=None)
        token = _progress_token(message) if max_timeout is not None else None
        loop = asyncio.get_running_loop()
        started = loop.time()
        limit = started + max(timeout, max_timeout or 0)
        deadline = started + timeout
        notifications: list[JsonObject] = []
        other: list[bytes] = []
        progress = 0
        while True:
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TransportTimeout("deadline passed")
                line = await self.stdio.receive(remaining)
            except TransportTimeout as exc:
                raise TransportTimeout(
                    f"no response with id {wanted!r} within {loop.time() - started:.0f}s",
                    progress=progress,
                ) from exc
            parsed = _parse(line)
            is_response = isinstance(parsed, dict) and "method" not in parsed and "id" in parsed
            if is_response and parsed["id"] == wanted:
                return Reply(parsed, tuple(notifications), tuple(other))
            if isinstance(parsed, dict) and "method" in parsed and "id" not in parsed:
                notifications.append(parsed)
                if _is_progress_for(token, parsed):
                    progress += 1
                    deadline = min(loop.time() + timeout, limit)
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
        self.legacy_version: str | None = None
        """Set once a legacy initialize handshake succeeded on this connection."""
        self.extra_secrets: set[str] = set()
        """Secrets the server revealed (SEC-003), masked in saved recordings too."""

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
        masker = Masker(self.extra_secrets)
        masker.add_headers(self.spec.headers)
        return masker

    @property
    def session_id(self) -> str | None:
        return self._session

    def conformant_headers(self, body: bytes) -> list[Header]:
        """Headers a conformant client of this connection's era sends.

        Modern: request metadata mirrored from the body. Legacy, after the
        handshake: ``MCP-Protocol-Version`` with the negotiated version and the
        session id, if the server assigned one.
        """
        headers: list[Header] = [("Content-Type", "application/json"), ("Accept", ACCEPT)]
        if self.legacy_version is None:
            headers.extend(request_metadata_headers(body))
        else:
            headers.append(("MCP-Protocol-Version", self.legacy_version))
        if self._session is not None:
            headers.append((LEGACY_SESSION_HEADER, self._session))
        return headers

    async def call(
        self, message: JsonObject | bytes, timeout: float, *, max_timeout: float | None = None
    ) -> Reply:
        """POST ``message``; see ``StdioConnection.call`` for ``max_timeout``."""
        body = encode(message)
        token = _progress_token(message) if max_timeout is not None else None
        progress = 0

        def is_progress(event: SseEvent) -> bool:
            nonlocal progress
            if _is_progress_for(token, _parse(event.data)):
                progress += 1
                return True
            return False

        try:
            exchange = await self.http.request(
                "POST",
                body,
                self.conformant_headers(body),
                timeout=timeout,
                extend=None if token is None else is_progress,
                max_timeout=max_timeout,
            )
        except TransportTimeout as exc:
            raise TransportTimeout(str(exc), progress=progress) from exc
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
