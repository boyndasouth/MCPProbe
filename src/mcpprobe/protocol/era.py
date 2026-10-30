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


async def detect_era(connection: Connection, timeout: float) -> ServerFacts:
    try:
        reply = await connection.call(request("server/discover", "mcpprobe-era-discover"), timeout)
    except TransportError:
        reply = None
    if reply is not None:
        result = reply.result
        if isinstance(result, dict) and "supportedVersions" in result:
            return ServerFacts(Era.MODERN, result)
        if _modern_error(reply):
            return ServerFacts(Era.MODERN)

    try:
        reply = await connection.call(request("tools/list", "mcpprobe-era-tools"), timeout)
    except TransportError:
        return ServerFacts(Era.NOT_MODERN)
    result = reply.result
    if (isinstance(result, dict) and "resultType" in result) or _modern_error(reply):
        return ServerFacts(Era.MODERN)
    return ServerFacts(Era.NOT_MODERN)
