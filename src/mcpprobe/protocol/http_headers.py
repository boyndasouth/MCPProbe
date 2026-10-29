"""Request metadata headers for Streamable HTTP (2026-07-28).

Spec: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#request-metadata

- ``MCP-Protocol-Version`` mirrors ``params._meta["io.modelcontextprotocol/protocolVersion"]``.
- ``Mcp-Method`` mirrors ``method`` on every request.
- ``Mcp-Name`` mirrors ``params.name`` (``tools/call``, ``prompts/get``) or
  ``params.uri`` (``resources/read``).
- Values that are not plain header-safe ASCII use the Base64 sentinel
  ``=?base64?...?=`` (see "Value Encoding").

This module builds *conformant* headers. Suites that test header validation
build wrong ones on purpose and do not use it.
"""

from __future__ import annotations

import base64
import json
from typing import Any

PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
NAME_SOURCE: dict[str, str] = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}
_SENTINEL_PREFIX = "=?base64?"
_SENTINEL_SUFFIX = "?="


def _header_safe(value: str) -> bool:
    if value != value.strip(" \t"):
        return False
    if value.startswith(_SENTINEL_PREFIX) and value.endswith(_SENTINEL_SUFFIX):
        return False
    return all(ch == "\t" or 0x20 <= ord(ch) <= 0x7E for ch in value)


def encode_header_value(value: str) -> str:
    """Plain value if header-safe, else the Base64 sentinel form."""
    if _header_safe(value):
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"{_SENTINEL_PREFIX}{encoded}{_SENTINEL_SUFFIX}"


def decode_header_value(value: str) -> str:
    if value.startswith(_SENTINEL_PREFIX) and value.endswith(_SENTINEL_SUFFIX):
        inner = value[len(_SENTINEL_PREFIX) : -len(_SENTINEL_SUFFIX)]
        return base64.b64decode(inner, validate=True).decode("utf-8")
    return value


def request_metadata_headers(body: bytes) -> list[tuple[str, str]]:
    """Headers a conformant client sends for this JSON-RPC body.

    Returns nothing for bodies that are not a JSON object; fields that are
    absent (for example no ``_meta`` on a legacy message) yield no header.
    """
    try:
        message: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(message, dict):
        return []
    headers: list[tuple[str, str]] = []
    method = message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    meta = params.get("_meta")
    version = meta.get(PROTOCOL_VERSION_META_KEY) if isinstance(meta, dict) else None
    if isinstance(version, str):
        headers.append(("MCP-Protocol-Version", version))
    if isinstance(method, str):
        headers.append(("Mcp-Method", method))
        source = NAME_SOURCE.get(method)
        name = params.get(source) if source else None
        if isinstance(name, str):
            headers.append(("Mcp-Name", encode_header_value(name)))
    return headers
