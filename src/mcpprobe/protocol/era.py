"""Which protocol era a server speaks, decided once per run.

Follows the 2026-07-28 backward-compatibility rules
(basic/versioning#backward-compatibility-with-initialization-based-versions):

- stdio: send ``server/discover``. A ``DiscoverResult`` or a recognized modern
  error means modern; any other error, or silence, means not modern. The
  fallback is never keyed to one specific error code.
- HTTP: a modern request that succeeds, or that fails with a recognized
  modern JSON-RPC error in the body, means modern.

One addition for conformance testing: a server that lacks ``server/discover``
(itself a violation, CONF-001) would look legacy by those rules. So when
discover fails, a modern ``tools/list`` is tried; a result carrying
``resultType``, or a modern error, still identifies a modern server. A legacy
server that answers ``tools/list`` without ``resultType`` stays legacy, so
this can only add findings for modern servers, never invent them for legacy
ones.

Full legacy and dual-era classification arrives in milestone M2.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mcpprobe.protocol.modern import METHOD_NOT_FOUND, MODERN_ERROR_CODES, request
from mcpprobe.transport.errors import TransportError

if TYPE_CHECKING:
    from mcpprobe.engine.connection import Connection, Reply


class Era(StrEnum):
    MODERN = "modern"
    NOT_MODERN = "not-modern"


@dataclass(frozen=True)
class ServerFacts:
    era: Era
    discover: dict[str, Any] | None = None
    """The DiscoverResult, when ``server/discover`` answered with one."""

    @property
    def capabilities_known(self) -> bool:
        return self.discover is not None

    @property
    def capabilities(self) -> dict[str, Any]:
        caps = (self.discover or {}).get("capabilities")
        return caps if isinstance(caps, dict) else {}


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


async def _discover(connection: Connection, request_id: str, timeout: float) -> Reply | None:
    try:
        return await connection.call(request("server/discover", request_id), timeout)
    except TransportError:
        return None


def _discover_result(reply: Reply | None) -> dict[str, Any] | None:
    result = None if reply is None else reply.result
    return result if isinstance(result, dict) and "supportedVersions" in result else None


async def detect_era(
    connection: Connection, timeout: float, startup_timeout: float | None = None
) -> ServerFacts:
    """``startup_timeout`` bounds the first request, which includes server start-up
    (a cold ``npx`` or ``uvx`` can take many seconds); later requests use ``timeout``."""
    reply = await _discover(connection, "mcpprobe-era-discover", startup_timeout or timeout)
    if (found := _discover_result(reply)) is not None:
        return ServerFacts(Era.MODERN, found)
    if reply is not None and _modern_error(reply):
        return ServerFacts(Era.MODERN)

    try:
        reply = await connection.call(request("tools/list", "mcpprobe-era-tools"), timeout)
    except TransportError:
        return ServerFacts(Era.NOT_MODERN)
    result = reply.result
    if not ((isinstance(result, dict) and "resultType" in result) or _modern_error(reply)):
        return ServerFacts(Era.NOT_MODERN)
    # Modern after all. If discover only missed its window, ask once more so
    # the checks get the real capabilities instead of an empty set.
    retry = await _discover(connection, "mcpprobe-era-discover-retry", timeout)
    return ServerFacts(Era.MODERN, _discover_result(retry))
