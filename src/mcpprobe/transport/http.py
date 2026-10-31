"""Raw Streamable HTTP transport over httpx (HTTP/1.1 and HTTP/2).

Sends exactly the method, headers and body it is given; adds no MCP headers of
its own. Suites decide what is conformant to send, including deliberately
wrong headers. Responses are read as raw bytes (``Accept-Encoding: identity``
so the recorded body is what the server meant to send) and, when the server
answers with ``text/event-stream``, parsed into SSE events as they arrive.

Closing a response stream early (``stop_after_events``) is how a client
cancels a request on Streamable HTTP in the 2026-07-28 revision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

import httpx

from mcpprobe import __version__
from mcpprobe.transport.errors import TransportClosed, TransportTimeout
from mcpprobe.transport.sse import SseEvent, SseParser

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType

    from mcpprobe.transport.frames import Header
    from mcpprobe.transport.recorder import Recorder

DEFAULT_MAX_BODY = 64 * 1024 * 1024

# Fixed client headers, the same on every request and not recorded per frame.
CLIENT_HEADERS: tuple[Header, ...] = (
    ("User-Agent", f"mcpprobe/{__version__}"),
    ("Accept-Encoding", "identity"),
)


@dataclass(frozen=True, slots=True)
class HttpExchange:
    exchange: int
    status: int
    http_version: str
    headers: tuple[Header, ...]
    body: bytes
    complete: bool
    events: tuple[SseEvent, ...] = field(default=())

    def header(self, name: str) -> str | None:
        """First value of a header, matched case-insensitively."""
        wanted = name.lower()
        return next((v for k, v in self.headers if k.lower() == wanted), None)

    @property
    def is_sse(self) -> bool:
        content_type = self.header("content-type") or ""
        return content_type.split(";")[0].strip().lower() == "text/event-stream"


class HttpTransport:
    def __init__(
        self,
        url: str,
        *,
        recorder: Recorder,
        headers: Sequence[Header] = (),
        timeout: float = 10.0,
        http2: bool = True,
        max_body_bytes: int = DEFAULT_MAX_BODY,
    ) -> None:
        self.url = url
        self._recorder = recorder
        self._headers = tuple(headers)
        self._timeout = timeout
        self._max_body = max_body_bytes
        self._client = httpx.AsyncClient(
            http2=http2,
            headers=list(CLIENT_HEADERS),
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def request(
        self,
        method: str = "POST",
        body: bytes = b"",
        headers: Sequence[Header] = (),
        *,
        url: str | None = None,
        stop_after_events: int | None = None,
        timeout: float | None = None,
        extend: Callable[[SseEvent], bool] | None = None,
        max_timeout: float | None = None,
    ) -> HttpExchange:
        """Send one HTTP request and read the whole response (or up to a stop).

        With ``extend``, every SSE event it accepts (a progress notification)
        restarts the timeout, up to ``max_timeout`` in total.
        """
        target = url or self.url
        all_headers = (*self._headers, *headers)
        exchange = self._recorder.new_exchange()
        self._recorder.http_request(exchange, method, target, all_headers, body, stop_after_events)

        response: httpx.Response | None = None
        chunks: list[bytes] = []
        events: list[SseEvent] = []
        complete = True
        loop = asyncio.get_running_loop()
        started = loop.time()
        window = timeout or self._timeout
        limit = started + max(window, max_timeout or 0)
        try:
            async with asyncio.timeout(window) as deadline:
                request = self._client.build_request(
                    method, target, content=body or None, headers=list(all_headers)
                )
                response = await self._client.send(request, stream=True)
                try:
                    is_sse = _is_sse(response)
                    parser = SseParser() if is_sse else None
                    size = 0
                    async for chunk in response.aiter_raw():
                        chunks.append(chunk)
                        size += len(chunk)
                        if parser is not None:
                            fresh = parser.feed(chunk)
                            events.extend(fresh)
                            if extend is not None and any(extend(e) for e in fresh):
                                deadline.reschedule(min(loop.time() + window, limit))
                            if stop_after_events is not None and len(events) >= stop_after_events:
                                complete = False
                                break
                        if size > self._max_body:
                            complete = False
                            break
                    if complete and parser is not None:
                        events.extend(parser.close())
                finally:
                    await response.aclose()
        except (TimeoutError, httpx.TimeoutException) as exc:
            self._record_partial(exchange, response, chunks)
            self._recorder.http_error(exchange, "timeout")
            raise TransportTimeout(f"{method} {target}: no complete response in time") from exc
        except httpx.HTTPError as exc:
            self._record_partial(exchange, response, chunks)
            message = f"{type(exc).__name__}: {exc}"
            self._recorder.http_error(exchange, message)
            raise TransportClosed(f"{method} {target}: {message}") from exc

        headers_out = _raw_headers(response)
        data = b"".join(chunks)
        self._recorder.http_response(
            exchange,
            response.status_code,
            response.http_version,
            headers_out,
            data,
            complete=complete,
        )
        return HttpExchange(
            exchange=exchange,
            status=response.status_code,
            http_version=response.http_version,
            headers=headers_out,
            body=data,
            complete=complete,
            events=tuple(events),
        )

    def _record_partial(
        self, exchange: int, response: httpx.Response | None, chunks: list[bytes]
    ) -> None:
        if response is not None:
            self._recorder.http_response(
                exchange,
                response.status_code,
                response.http_version,
                _raw_headers(response),
                b"".join(chunks),
                complete=False,
            )


def _is_sse(response: httpx.Response) -> bool:
    content_type = str(response.headers.get("content-type", ""))
    return content_type.split(";")[0].strip().lower() == "text/event-stream"


def _raw_headers(response: httpx.Response) -> tuple[Header, ...]:
    return tuple(
        (name.decode("latin-1"), value.decode("latin-1")) for name, value in response.headers.raw
    )
