"""Which protocol era(s) a server speaks, decided once per run.

Spec: 2026-07-28 basic/versioning#backward-compatibility-with-initialization-based-versions
and the stdio / Streamable HTTP "Backward Compatibility" sections.

1. Send ``server/discover`` first (stdio requires it before anything else).
   A DiscoverResult, or a recognized modern JSON-RPC error, means modern.
   The fallback is never keyed to one specific error code, and silence also
   means "not modern".
2. Not modern: try ``initialize`` on the same connection. An InitializeResult
   means legacy.
3. Neither: a modern ``tools/list`` catches modern servers that lack
   ``server/discover`` (itself a CONF-001 violation). A result carrying
   ``resultType``, or a modern error, means modern; that can only add
   findings for modern servers, never invent them for legacy ones.
4. Modern: try ``initialize`` on a *fresh* connection (a new process for
   stdio, a new session for HTTP), so the probe cannot disturb the modern
   connection. An InitializeResult there means dual-era.

Anything else is ``unknown``: the server answered, but neither era's way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mcpprobe.protocol import legacy
from mcpprobe.protocol.modern import METHOD_NOT_FOUND, MODERN_ERROR_CODES, request
from mcpprobe.transport.errors import TransportError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcpprobe.engine.connection import Connection, Reply


class Era(StrEnum):
    MODERN = "modern"
    LEGACY = "legacy"
    DUAL = "dual"
    UNKNOWN = "unknown"

    @property
    def speaks_modern(self) -> bool:
        return self in (Era.MODERN, Era.DUAL)

    @property
    def speaks_legacy(self) -> bool:
        return self in (Era.LEGACY, Era.DUAL)


@dataclass(frozen=True)
class ServerFacts:
    era: Era
    discover: dict[str, Any] | None = None
    """The DiscoverResult, when ``server/discover`` answered with one."""
    initialize: dict[str, Any] | None = None
    """The InitializeResult, when ``initialize`` answered with one."""
    auth_required: bool = False
    """The first request got HTTP 401: the server wants credentials first."""

    @property
    def capabilities_known(self) -> bool:
        return self.discover is not None

    @property
    def capabilities(self) -> dict[str, Any]:
        """Modern capabilities, from server/discover."""
        caps = (self.discover or {}).get("capabilities")
        return caps if isinstance(caps, dict) else {}

    @property
    def legacy_capabilities(self) -> dict[str, Any]:
        caps = (self.initialize or {}).get("capabilities")
        return caps if isinstance(caps, dict) else {}

    @property
    def legacy_version(self) -> str | None:
        version = (self.initialize or {}).get("protocolVersion")
        return version if isinstance(version, str) else None


def _modern_error(reply: Reply) -> bool:
    error = reply.error
    if error is None:
        return False
    code = error.get("code")
    if code in MODERN_ERROR_CODES:
        return True
    # On HTTP a modern server answers an unknown method with 404 plus a
    # JSON-RPC -32601 body (streamable-http#protocol-version-header).
    return reply.http_status == 404 and code == METHOD_NOT_FOUND


async def _call(connection: Connection, message: dict[str, Any], timeout: float) -> Reply | None:
    try:
        return await connection.call(message, timeout)
    except TransportError:
        return None


def _discover_result(reply: Reply | None) -> dict[str, Any] | None:
    result = None if reply is None else reply.result
    return result if isinstance(result, dict) and "supportedVersions" in result else None


def _initialize_result(reply: Reply | None) -> dict[str, Any] | None:
    if legacy.negotiated_version(reply) is None:
        return None
    assert reply is not None
    result = reply.result
    return result if isinstance(result, dict) else None


async def _legacy_probe(connection: Connection, timeout: float) -> dict[str, Any] | None:
    try:
        reply = await legacy.open_session(connection, timeout, request_id="mcpprobe-era-initialize")
    except TransportError:
        return None
    return _initialize_result(reply)


async def detect_era(
    connection: Connection,
    timeout: float,
    startup_timeout: float | None = None,
    open_fresh: Callable[[], Awaitable[Connection]] | None = None,
) -> ServerFacts:
    """Classify the server. ``startup_timeout`` bounds the first request, which
    includes start-up (a cold ``npx`` or ``uvx`` can take many seconds).
    ``open_fresh`` opens a new, started connection for the dual-era probe;
    without it a modern server is reported as modern only."""
    reply = await _call(
        connection, request("server/discover", "mcpprobe-era-discover"), startup_timeout or timeout
    )
    if reply is not None and reply.http_status == 401:
        return ServerFacts(Era.UNKNOWN, auth_required=True)
    discover = _discover_result(reply)
    modern = discover is not None or (reply is not None and _modern_error(reply))

    if not modern:
        initialize = await _legacy_probe(connection, timeout)
        if initialize is not None:
            return ServerFacts(Era.LEGACY, initialize=initialize)
        reply = await _call(connection, request("tools/list", "mcpprobe-era-tools"), timeout)
        result = None if reply is None else reply.result
        modern = reply is not None and (
            (isinstance(result, dict) and "resultType" in result) or _modern_error(reply)
        )
        if not modern:
            return ServerFacts(Era.UNKNOWN)
        # Modern after all. If discover only missed its window, ask once more so
        # the checks get the real capabilities instead of an empty set.
        retry = await _call(
            connection, request("server/discover", "mcpprobe-era-discover-retry"), timeout
        )
        discover = _discover_result(retry)

    if open_fresh is None:
        return ServerFacts(Era.MODERN, discover)
    fresh = await open_fresh()
    try:
        initialize = await _legacy_probe(fresh, startup_timeout or timeout)
    finally:
        await fresh.close()
    if initialize is not None:
        return ServerFacts(Era.DUAL, discover, initialize)
    return ServerFacts(Era.MODERN, discover)
