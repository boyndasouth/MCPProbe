"""Building blocks for modern-era (2026-07-28) requests.

Spec: https://modelcontextprotocol.io/specification/2026-07-28/basic/index#meta

Every modern request carries ``_meta`` with the protocol version and client
capabilities (both required) and, by convention, client info.
"""

from __future__ import annotations

from typing import Any

from mcpprobe import __version__

VERSION = "2026-07-28"

PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Codes the 2026-07-28 spec reserves and defines (basic/index#error-codes).
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
MODERN_ERROR_CODES = frozenset(
    {HEADER_MISMATCH, MISSING_REQUIRED_CLIENT_CAPABILITY, UNSUPPORTED_PROTOCOL_VERSION}
)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

CORE_RESULT_TYPES = frozenset({"complete", "input_required"})

_UNSET: Any = object()


def meta(
    *,
    version: str = VERSION,
    capabilities: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Request ``_meta``; ``drop`` removes keys to build malformed requests."""
    fields: dict[str, Any] = {
        PROTOCOL_VERSION: version,
        CLIENT_INFO: {"name": "mcpprobe", "version": __version__},
        CLIENT_CAPABILITIES: capabilities if capabilities is not None else {},
    }
    for key in drop:
        fields.pop(key, None)
    return fields


def request(
    method: str,
    request_id: Any,
    params: dict[str, Any] | None = None,
    *,
    request_meta: Any = _UNSET,
) -> dict[str, Any]:
    """A JSON-RPC request with modern ``_meta`` merged into ``params``."""
    body: dict[str, Any] = dict(params or {})
    body["_meta"] = meta() if request_meta is _UNSET else request_meta
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": body}
