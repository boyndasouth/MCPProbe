from __future__ import annotations

import json

import pytest

from mcpprobe.transport.errors import TransportClosed, TransportTimeout
from mcpprobe.transport.frames import HttpErrorFrame, HttpResponseFrame
from mcpprobe.transport.http import HttpTransport
from mcpprobe.transport.recorder import Recorder


def _body(method: str, mid: int | None = 1, **params: object) -> bytes:
    msg: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
    if mid is not None:
        msg["id"] = mid
    return json.dumps(msg).encode()


async def test_json_response_and_headers_sent_verbatim(http_url: str) -> None:
    recorder = Recorder()
    async with HttpTransport(http_url, recorder=recorder) as transport:
        exchange = await transport.request(
            "POST", _body("tools/call"), [("Mcp-Method", "tools/call"), ("Mcp-Name", "wrong")]
        )
    assert exchange.status == 200
    assert not exchange.is_sse
    result = json.loads(exchange.body)["result"]
    assert result["mcp_method_header"] == "tools/call"
    assert result["mcp_name_header"] == "wrong"  # the transport never "fixes" headers
    assert exchange.header("CONTENT-TYPE") == "application/json"
    assert exchange.header("x-missing") is None
    response = next(f for f in recorder.frames if isinstance(f, HttpResponseFrame))
    assert response.complete
    assert response.body == exchange.body


async def test_notification_gets_202_and_get_gets_405(http_url: str) -> None:
    async with HttpTransport(http_url, recorder=Recorder()) as transport:
        assert (await transport.request("POST", _body("note", mid=None))).status == 202
        assert (await transport.request("GET")).status == 405


async def test_sse_stream_is_parsed(http_url: str) -> None:
    async with HttpTransport(http_url, recorder=Recorder()) as transport:
        exchange = await transport.request("POST", _body("stream"))
    assert exchange.is_sse
    assert exchange.complete
    assert [json.loads(e.data).get("params", {}).get("n") for e in exchange.events] == [
        1,
        2,
        3,
        None,
    ]
    assert json.loads(exchange.events[-1].data)["result"] == {"done": True}


async def test_stop_after_events_closes_stream_early(http_url: str) -> None:
    recorder = Recorder()
    async with HttpTransport(http_url, recorder=recorder) as transport:
        exchange = await transport.request("POST", _body("endless"), stop_after_events=2)
    assert not exchange.complete
    assert len(exchange.events) >= 2
    response = next(f for f in recorder.frames if isinstance(f, HttpResponseFrame))
    assert not response.complete


async def test_body_size_cap(http_url: str) -> None:
    async with HttpTransport(http_url, recorder=Recorder(), max_body_bytes=10) as transport:
        exchange = await transport.request("POST", _body("echo", pad="x" * 100))
    assert not exchange.complete


async def test_timeout_is_recorded(http_url: str) -> None:
    recorder = Recorder()
    async with HttpTransport(http_url, recorder=recorder, timeout=0.3) as transport:
        with pytest.raises(TransportTimeout):
            await transport.request("POST", _body("sleep", seconds=2))
    assert any(isinstance(f, HttpErrorFrame) and f.error == "timeout" for f in recorder.frames)


async def test_timeout_mid_stream_records_partial_body(http_url: str) -> None:
    recorder = Recorder()
    async with HttpTransport(http_url, recorder=recorder) as transport:
        with pytest.raises(TransportTimeout):
            await transport.request("POST", _body("endless"), timeout=0.3)
    partial = next(f for f in recorder.frames if isinstance(f, HttpResponseFrame))
    assert not partial.complete
    assert b"notifications/progress" in partial.body


async def test_unreachable_server() -> None:
    recorder = Recorder()
    async with HttpTransport("http://127.0.0.1:9/mcp", recorder=recorder) as transport:
        with pytest.raises(TransportClosed):
            await transport.request("POST", b"{}")
    assert any(isinstance(f, HttpErrorFrame) for f in recorder.frames)
