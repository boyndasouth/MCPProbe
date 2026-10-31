from __future__ import annotations

import asyncio
import json
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
from tests.support.procs import pid_alive


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


def _wait_dead(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


async def test_close_kills_grandchildren(stdio_argv: list[str]) -> None:
    async with StdioTransport(stdio_argv, recorder=Recorder()) as transport:
        await transport.send_line(_msg("grandchild"))
        pid = json.loads(await transport.receive(5))["result"]["pid"]
        assert pid_alive(pid)
    assert _wait_dead(pid), "grandchild survived close()"


ORPHANING_WRAPPER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "print(child.pid, flush=True)\n"
)


async def test_close_kills_orphans_of_a_wrapper_that_exited() -> None:
    """The npx case: the wrapper exits, its child keeps our stdout pipe open.

    POSIX process groups and the Windows Job Object must both still reach it.
    """
    transport = StdioTransport([sys.executable, "-c", ORPHANING_WRAPPER], recorder=Recorder())
    await transport.start()
    pid = int(await transport.receive(10))
    assert await transport.wait_for_exit(10) == 0  # the wrapper is gone
    assert pid_alive(pid)  # ...but its child lives on
    await transport.close(grace=0.5)
    assert _wait_dead(pid), "orphaned child survived close()"


def test_job_is_inert_off_windows() -> None:
    from mcpprobe.transport.winjob import ProcessJob, new_job

    job = new_job()
    if sys.platform != "win32":
        assert type(job) is ProcessJob
        assert job.assign(1) is False
    job.terminate()
    job.close()
    job.close()  # idempotent


def test_split_command_and_resolve() -> None:
    assert split_command('python -m "my server"') == ["python", "-m", "my server"]
    # Windows paths are case-insensitive and may come back as python3.EXE.
    assert resolve_argv(["python3", "-V"])[0].lower().endswith(("python3", "python3.exe"))
    assert resolve_argv(["./not-on-path"]) == ["./not-on-path"]
    with pytest.raises(ValueError, match="empty"):
        resolve_argv([])


def test_grow_tree_follows_children_and_rejects_impostors() -> None:
    from mcpprobe.transport.winjob import grow_tree

    # 10 is the server (started at t=100). 11 and 12 are its children; 13 is a
    # grandchild under 11. 14 names 12 as parent but started before 12 did, so
    # it is some older process: not a descendant. 99 is MCPProbe itself and is
    # never included. 15 cannot be read.
    children = {10: [11, 12, 99], 11: [13], 12: [14], 13: [15]}
    started = {10: 100, 11: 110, 12: 120, 13: 130, 14: 50, 99: 105}

    found = grow_tree(children, started.get, {10: 100}, exclude=99)
    assert found == {10: 100, 11: 110, 12: 120, 13: 130}


def test_grow_tree_reaches_children_of_exited_middle_processes() -> None:
    """The Store-Python case: the middle of the chain exits first.

    Seen earlier: server 10 -> launcher 11. Now 11 has exited (it can no longer
    be read), but its child 12 still names it as parent and must be found.
    """
    from mcpprobe.transport.winjob import grow_tree, still_running

    known = {10: 100, 11: 110}
    children = {11: [12]}  # 11 itself is gone from the process table
    started = {10: 100, 12: 120}
    found = grow_tree(children, started.get, known, exclude=0)
    assert found == {10: 100, 11: 110, 12: 120}
    assert sorted(still_running(started.get, found)) == [10, 12]


def test_grow_tree_ignores_children_of_a_recycled_pid() -> None:
    # 11 was ours (born 110) and exited; Windows gave PID 11 to an unrelated
    # process born at 500. Its child 13 (born 510) is not ours. Our old child
    # 12 (born 120, before the impostor) still is.
    from mcpprobe.transport.winjob import grow_tree, still_running

    children = {11: [12, 13]}
    started = {11: 500, 12: 120, 13: 510}
    found = grow_tree(children, started.get, {11: 110}, exclude=0)
    assert found == {11: 110, 12: 120}
    # ...and the impostor now holding PID 11 is never killed.
    assert still_running(started.get, found) == [12]


def test_grow_tree_survives_cycles() -> None:
    from mcpprobe.transport.winjob import grow_tree

    children = {1: [2], 2: [3], 3: [2, 1]}
    started = {1: 10, 2: 20, 3: 30}
    assert grow_tree(children, started.get, {1: 10}, exclude=0) == {1: 10, 2: 20, 3: 30}
    assert grow_tree({}, started.get, {1: 10}, exclude=0) == {1: 10}


async def test_tree_tracker_runs_while_server_lives(
    stdio_argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows tracker's lifecycle, with a fake job: it refreshes repeatedly
    while the server runs, the final terminate happens, then it stops."""
    from mcpprobe.transport import stdio as stdio_module
    from mcpprobe.transport.winjob import ProcessJob

    events: list[str] = []

    class FakeJob(ProcessJob):
        tracks = True

        def refresh(self) -> None:
            events.append("refresh")

        def terminate(self) -> None:
            events.append("terminate")

    from types import SimpleNamespace

    from mcpprobe.transport import process as process_module

    monkeypatch.setattr(stdio_module, "new_job", FakeJob)
    # Route terminate_tree down its Windows branch (job.terminate) without
    # pretending the whole interpreter is on Windows. The server still exits
    # on its own when its stdin closes.
    monkeypatch.setattr(process_module, "sys", SimpleNamespace(platform="win32"))
    transport = StdioTransport(stdio_argv, recorder=Recorder())
    await transport.start()
    await asyncio.sleep(0.3)
    await transport.close(grace=1)
    refreshes_at_close = events.count("refresh")
    assert refreshes_at_close >= 3
    assert "terminate" in events
    await asyncio.sleep(0.3)
    assert events.count("refresh") == refreshes_at_close  # stopped for good
