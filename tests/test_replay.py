"""M0 gate: a recorded session replays byte for byte."""

from __future__ import annotations

import json

import pytest

from mcpprobe.engine.record import record_http, record_stdio, request_id, response_id
from mcpprobe.engine.replay import ReplayError, replay
from mcpprobe.transport.frames import HttpRequestFrame, Recording, StdioFrame, StdioTarget
from tests.support.http_server import TOKEN


def _msg(method: str, mid: int | None, **params: object) -> bytes:
    body: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
    if mid is not None:
        body["id"] = mid
    return json.dumps(body).encode()


STDIO_SCRIPT = [
    _msg("echo", 1, a=1),
    _msg("notifications/initialized", None),
    _msg("notify", 2),
    b"{oops not json",
    _msg("log", 3),
    _msg("echo", 4, text="héllo ✓"),
]


async def test_stdio_session_replays_byte_for_byte(stdio_argv: list[str]) -> None:
    result = await record_stdio(stdio_argv, STDIO_SCRIPT, timeout=5)
    assert result.sent == len(STDIO_SCRIPT)
    assert result.answered == 4
    assert result.problems == []

    # Survive a save/load round trip first, exactly like `mcpprobe replay` does.
    original = Recording.loads(result.recording.dumps())
    replayed = await replay(original, timeout=5)

    assert replayed.matched, replayed.differences
    sent = [f.data for f in original.stdio_frames("stdin")]
    assert [f.data for f in replayed.replayed.stdio_frames("stdin")] == sent
    assert sent == [m + b"\n" for m in STDIO_SCRIPT]


async def test_stdio_crash_is_part_of_the_recording(stdio_argv: list[str]) -> None:
    result = await record_stdio(stdio_argv, [_msg("echo", 1), _msg("crash", 2)], timeout=2)
    assert result.problems  # the crash request never got a response
    replayed = await replay(result.recording, timeout=5)
    assert replayed.matched, replayed.differences


async def test_stdio_difference_is_reported(stdio_argv: list[str]) -> None:
    result = await record_stdio(stdio_argv, [_msg("echo", 1, v=1)], timeout=5)
    frames = list(result.recording.frames)
    index = next(
        i for i, f in enumerate(frames) if isinstance(f, StdioFrame) and f.stream == "stdout"
    )
    tampered = frames[index]
    assert isinstance(tampered, StdioFrame)
    frames[index] = tampered.model_copy(update={"data": b'{"changed":true}\n'})
    doctored = result.recording.model_copy(update={"frames": tuple(frames)})
    replayed = await replay(doctored, timeout=5)
    assert not replayed.matched
    assert "stdout #1 differs" in replayed.differences[0]


async def test_stdio_env_is_masked_and_required(stdio_argv: list[str]) -> None:
    env = {"PROBE_SECRET": "hunter2-secret"}
    result = await record_stdio(stdio_argv, [_msg("secret", 1)], env=env, timeout=5)
    text = result.recording.dumps()
    assert "hunter2-secret" not in text
    assert "<masked>" in text
    assert isinstance(result.recording.target, StdioTarget)
    assert result.recording.target.env_keys == ("PROBE_SECRET",)

    with pytest.raises(ReplayError, match="PROBE_SECRET"):
        await replay(result.recording, timeout=5)
    replayed = await replay(result.recording, env=env, timeout=5)
    assert replayed.matched, replayed.differences


async def test_masked_stdin_cannot_be_replayed(stdio_argv: list[str]) -> None:
    env = {"PROBE_SECRET": "hunter2-secret"}
    result = await record_stdio(stdio_argv, [_msg("echo", 1, s="hunter2-secret")], env=env)
    with pytest.raises(ReplayError, match="masked"):
        await replay(result.recording, env=env)


HTTP_SCRIPT = [
    _msg("initialize", 1),
    _msg("notifications/initialized", None),
    _msg("tools/call", 2, name="get_weather", arguments={}),
    _msg("stream", 3),
]


async def test_http_session_replays_with_new_session_id(http_url: str) -> None:
    headers = [("Authorization", TOKEN)]
    result = await record_http(http_url, HTTP_SCRIPT, headers=headers, timeout=5)
    assert result.problems == []
    assert result.answered == 3
    text = result.recording.dumps()
    assert "s3cret-token-value" not in text

    requests = [f for f in result.recording.frames if isinstance(f, HttpRequestFrame)]
    assert ("Mcp-Name", "get_weather") in requests[2].headers
    assert any(k == "Mcp-Session-Id" for k, _ in requests[2].headers)

    with pytest.raises(ReplayError, match="Authorization"):
        await replay(result.recording)
    replayed = await replay(Recording.loads(text), headers=headers, timeout=5)
    assert replayed.matched, replayed.differences
    new_requests = [f for f in replayed.replayed.frames if isinstance(f, HttpRequestFrame)]
    assert [f.body for f in new_requests] == [f.body for f in requests]


async def test_http_problems_are_reported(http_url: str) -> None:
    result = await record_http(
        http_url, [_msg("echo", 1)], headers=[("Authorization", "Bearer wrong-token")]
    )
    assert result.problems == ["message 1: HTTP 401"]


async def test_http_unreachable_records_problem() -> None:
    result = await record_http("http://127.0.0.1:9/mcp", [_msg("echo", 1), _msg("echo", 2)])
    assert len(result.problems) == 1


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b'{"id":1,"method":"x"}', (True, 1)),
        (b'{"method":"x"}', (False, None)),
        (b"nope", (False, None)),
    ],
)
def test_request_id(line: bytes, expected: tuple[bool, object]) -> None:
    assert request_id(line) == expected


def test_response_id() -> None:
    assert response_id(b'{"id":"a","result":{}}') == (True, "a")
    assert response_id(b'{"id":1,"method":"x"}') == (False, None)
    assert response_id(b"\xff") == (False, None)


def test_http_response_comparison() -> None:
    from mcpprobe.engine.replay import _compare_responses
    from mcpprobe.transport.frames import HttpResponseFrame

    def response(
        headers: dict[str, str] | None = None, *, status: int = 200, body: bytes = b"{}"
    ) -> HttpResponseFrame:
        return HttpResponseFrame(
            seq=0,
            exchange=0,
            status=status,
            http_version="HTTP/1.1",
            headers=tuple((headers or {}).items()),
            body=body,
            complete=True,
        )

    base = response({"date": "Mon", "content-type": "application/json"})
    same_but_volatile = response({"date": "Tue", "content-type": "application/json"})
    assert _compare_responses(1, base, same_but_volatile, None) == []
    assert _compare_responses(1, None, None, None) == []
    assert "missing" in _compare_responses(1, base, None, None)[0]
    assert "appeared" in _compare_responses(1, None, base, None)[0]
    diffs = _compare_responses(
        1, base, response({"content-type": "text/plain"}, status=500, body=b"x"), None
    )
    assert [d.split(":")[1].strip().split()[0] for d in diffs] == ["status", "response", "body"]

    sse_a = response(body=b"data: 1\n\ndata: 2\n\n")
    sse_b = response(body=b"data: 1\n\ndata: 3\n\n")
    assert _compare_responses(1, sse_a, sse_b, 1) == []
    assert "SSE" in _compare_responses(1, sse_a, sse_b, 2)[0]
