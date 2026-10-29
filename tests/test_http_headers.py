"""Examples taken from the 2026-07-28 Streamable HTTP spec, "Value Encoding"."""

from __future__ import annotations

import json

import pytest

from mcpprobe.protocol.http_headers import (
    decode_header_value,
    encode_header_value,
    request_metadata_headers,
)


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("us-west1", "us-west1"),
        ("Hello, 世界", "=?base64?SGVsbG8sIOS4lueVjA==?="),
        (" padded ", "=?base64?IHBhZGRlZCA=?="),
        ("line1\nline2", "=?base64?bGluZTEKbGluZTI=?="),
        ("=?base64?literal?=", "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="),
    ],
)
def test_spec_encoding_examples(value: str, encoded: str) -> None:
    assert encode_header_value(value) == encoded
    assert decode_header_value(encoded) == value


def _meta() -> dict[str, object]:
    return {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}


def test_tools_call_headers() -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_weather", "arguments": {}, "_meta": _meta()},
    }
    assert request_metadata_headers(json.dumps(body).encode()) == [
        ("MCP-Protocol-Version", "2026-07-28"),
        ("Mcp-Method", "tools/call"),
        ("Mcp-Name", "get_weather"),
    ]


def test_resources_read_uses_uri() -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "resources/read",
        "params": {"uri": "file:///projects/myapp/config.json", "_meta": _meta()},
    }
    headers = dict(request_metadata_headers(json.dumps(body).encode()))
    assert headers["Mcp-Name"] == "file:///projects/myapp/config.json"


def test_legacy_message_without_meta_gets_only_method() -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert request_metadata_headers(json.dumps(body).encode()) == [("Mcp-Method", "initialize")]


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b'{"params": 5}'])
def test_non_messages_yield_nothing(body: bytes) -> None:
    assert request_metadata_headers(body) == []
