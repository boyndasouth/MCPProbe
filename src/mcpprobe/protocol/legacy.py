"""Legacy (``initialize``-era) protocol: handshake and plain requests.

Spec: https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle

- The client sends ``initialize`` with ``protocolVersion``, ``capabilities``
  and ``clientInfo``; the server answers with its own; the client then sends
  ``notifications/initialized``.
- On Streamable HTTP the server MAY assign ``MCP-Session-Id`` on the
  InitializeResult response, and the client MUST send ``MCP-Protocol-Version``
  on every request after initialization
  (2025-11-25/basic/transports#protocol-version-header).

MCPProbe always declares **no** client capabilities in a legacy session, so a
server MUST NOT send it sampling, elicitation or roots requests it could not
answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcpprobe import __version__

if TYPE_CHECKING:
    from mcpprobe.engine.connection import Connection, Reply

LATEST = "2025-11-25"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
"""Every initialize-era revision, newest first."""
OUTDATED = frozenset({"2025-03-26", "2024-11-05"})
"""Detect-and-warn only, per the MCPProbe revision table."""


def initialize(request_id: Any, version: str = LATEST) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "mcpprobe", "version": __version__},
        },
    }


INITIALIZED: dict[str, Any] = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def request(method: str, request_id: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        message["params"] = params
    return message


def negotiated_version(reply: Reply | None) -> str | None:
    """The protocolVersion of an InitializeResult, if the reply is one."""
    result = None if reply is None else reply.result
    version = result.get("protocolVersion") if isinstance(result, dict) else None
    return version if isinstance(version, str) else None


async def open_session(
    connection: Connection, timeout: float, *, request_id: str = "mcpprobe-initialize"
) -> Reply:
    """Run the handshake and switch the connection into legacy mode.

    Returns the initialize reply. The connection is in legacy mode only if the
    reply carried a protocolVersion; otherwise it is left untouched.
    """
    reply = await connection.call(initialize(request_id), timeout)
    version = negotiated_version(reply)
    if version is not None:
        connection.legacy_version = version
        await connection.call(INITIALIZED, timeout)
    return reply
