from __future__ import annotations

import json
import os
import sys
import time

import pytest

from mcpprobe.transport.errors import (
    LineTooLong,
    ServerStartError,
    TransportClosed,
    TransportTimeout,
)
from mcpprobe.transport.frames import ExitFrame
from mcpprobe.transport.process import resolve_argv, split_command
from mcpprobe.transport.recorder import Recorder
from mcpprobe.transport.stdio import StdioTransport


def _msg(method: str, mid: int | None = 1, **params: object) -> bytes:
    body: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
    if mid is not None:
        body["id"] = mid
    return json.dumps(body).encode()


async def test_send_and_receive_records_exact_bytes(stdio_argv: list[str]) -> None:
    recorder = Recorder()
    async with StdioTransport(stdio_argv, recorder=recorder) as transport:
        await transport.send_line(_msg("echo", x=1))
        line = await transport.receive(5)
    assert json.loads(line) == {"id": 1, "jsonrpc": "2.0", "result": {"echo": {"x": 1}}}
    stdin = [f for f in recorder.frames if f.kind == "stdio" and f.stream == "stdin"]
    assert [f.data for f in stdin] == [_msg("echo", x=1) + b"\n"]
    assert not any(isinstance(f, ExitFrame) for f in recorder.frames)


async def test_malformed_input_is_sent_verbatim(stdio_argv: list[str]) -> None:
    recorder = Recorder()
    async with StdioTransport(stdio_argv, recorder=recorder) as transport:
        await transport.send(b"{not json\n")
        line = await transport.receive(5)
    assert json.loads(line)["error"]["code"] == -32700


async def test_notifications_and_stderr(stdio_argv: list[str]) -> None:
    recorder = Recorder()
    async with StdioTransport(stdio_argv, recorder=recorder) as transport:
        await transport.send_line(_msg("notify"))
        first = await transport.receive(5)
        second = await transport.receive(5)
        await transport.send_line(_msg("log", mid=2))
        await transport.receive(5)
    assert json.loads(first)["method"] == "notifications/progress"
    assert json.loads(second)["id"] == 1
    assert "server log line" in transport.stderr_tail


async def test_crash_keeps_partial_line_and_exit_code(stdio_argv: list[str]) -> None:
    recorder = Recorder()
    async with StdioTransport(stdio_argv, recorder=recorder) as transport:
        await transport.send_line(_msg("crash"))
        assert await transport.receive(5) == b'{"partial":'
        with pytest.raises(TransportClosed):
            await transport.receive(5)
        with pytest.raises(TransportClosed):  # EOF is sticky
            await transport.receive(5)
        with pytest.raises(TransportClosed):
            await transport.send_line(_msg("echo"))
    exits = [f for f in recorder.frames if isinstance(f, ExitFrame)]
    assert [f.returncode for f in exits] == [3]


async def test_receive_timeout(stdio_argv: list[str]) -> None:
    async with StdioTransport(stdio_argv, recorder=Recorder()) as transport:
        await transport.send_line(_msg("silent"))
        with pytest.raises(TransportTimeout):
            await transport.receive(0.2)
        assert not await transport.wait_for_stdout(1, 0.1)


async def test_large_line_and_size_cap(stdio_argv: list[str]) -> None:
    async with StdioTransport(stdio_argv, recorder=Recorder()) as transport:
        await transport.send_line(_msg("big", size=1_000_000))
        assert len(await transport.receive(10)) > 1_000_000
    async with StdioTransport(stdio_argv, recorder=Recorder(), max_line_bytes=1000) as transport:
        await transport.send_line(_msg("big", size=200_000))
        with pytest.raises(LineTooLong):
            await transport.receive(10)


async def test_env_is_passed(stdio_argv: list[str]) -> None:
    env = {"PROBE_SECRET": "hunter2-value"}
    async with StdioTransport(stdio_argv, recorder=Recorder(), env=env) as transport:
        await transport.send_line(_msg("secret"))
        assert b"hunter2-value" in await transport.receive(5)


async def test_start_failure() -> None:
    transport = StdioTransport(["definitely-not-a-real-program-xyz"], recorder=Recorder())
    with pytest.raises(ServerStartError):
        await transport.start()
    assert await transport.close() is None


async def test_use_before_start_is_an_error() -> None:
    transport = StdioTransport(["x"], recorder=Recorder())
    assert transport.returncode is None
    with pytest.raises(RuntimeError):
        await transport.send(b"x")


async def test_close_escalates_when_stdin_is_ignored() -> None:
    stubborn = [
        sys.executable,
        "-c",
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN) if hasattr(signal, 'SIGTERM') else None\n"
        "sys.stdin.close()\n"
        "time.sleep(60)",
    ]
    transport = StdioTransport(stubborn, recorder=Recorder())
    await transport.start()
    started = time.monotonic()
    await transport.close(grace=0.3)
    assert transport.returncode is not None
    assert time.monotonic() - started < 5
    assert await transport.close() == transport.returncode  # idempotent


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_close_kills_grandchildren(stdio_argv: list[str]) -> None:
    async with StdioTransport(stdio_argv, recorder=Recorder()) as transport:
        await transport.send_line(_msg("grandchild"))
        pid = json.loads(await transport.receive(5))["result"]["pid"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("grandchild survived close()")


def test_split_command_and_resolve() -> None:
    assert split_command('python -m "my server"') == ["python", "-m", "my server"]
    # Windows paths are case-insensitive and may come back as python3.EXE.
    assert resolve_argv(["python3", "-V"])[0].lower().endswith(("python3", "python3.exe"))
    assert resolve_argv(["./not-on-path"]) == ["./not-on-path"]
    with pytest.raises(ValueError, match="empty"):
        resolve_argv([])
