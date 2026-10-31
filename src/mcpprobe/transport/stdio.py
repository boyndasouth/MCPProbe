"""Raw stdio transport: bytes in, newline-delimited lines out, everything recorded.

Nothing here understands JSON-RPC. ``send`` writes exactly the bytes it is
given (so suites can send malformed framing on purpose) and ``receive``
returns stdout one line at a time, exactly as written by the server.

Shutdown follows the spec's stdio "Shutdown" section: close stdin, wait,
then escalate to TERM and KILL, always across the whole process tree.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections import deque
from typing import TYPE_CHECKING, Literal, Self

from mcpprobe.transport.errors import (
    LineTooLong,
    ServerStartError,
    TransportClosed,
    TransportTimeout,
)
from mcpprobe.transport.process import resolve_argv, spawn_kwargs, terminate_tree
from mcpprobe.transport.winjob import ProcessJob, new_job

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    from mcpprobe.transport.recorder import Recorder

READ_CHUNK = 64 * 1024
DEFAULT_MAX_LINE = 64 * 1024 * 1024
STDERR_TAIL = 50


class _Eof:
    pass


_EOF = _Eof()


class StdioTransport:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        recorder: Recorder,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        max_line_bytes: int = DEFAULT_MAX_LINE,
    ) -> None:
        self.argv = tuple(argv)
        self._recorder = recorder
        self._env = dict(env or {})
        self._cwd = cwd
        self._max_line = max_line_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._lines: asyncio.Queue[bytes | _Eof | Exception] = asyncio.Queue()
        self._readers: list[asyncio.Task[None]] = []
        self._closing = False
        self._stdout_count = 0
        self._stdout_done = False
        self._count_changed = asyncio.Condition()
        self._job: ProcessJob = new_job()
        self._tracker: asyncio.Task[None] | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL)
        """Last stderr lines, for error messages when the server dies."""

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        env = {**os.environ, **self._env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                *resolve_argv(self.argv),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._cwd,
                **spawn_kwargs(),
            )
        except (OSError, ValueError) as exc:
            raise ServerStartError(f"could not start {self.argv[0]!r}: {exc}") from exc
        self._job.assign(self._process.pid)
        if self._job.tracks:  # pragma: no cover - Windows only
            self._tracker = asyncio.create_task(self._track_tree())
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._readers = [
            asyncio.create_task(self._read_stdout(self._process.stdout)),
            asyncio.create_task(self._read_stderr(self._process.stderr)),
        ]

    async def _track_tree(self) -> None:  # pragma: no cover - Windows only
        """Keep the job's picture of the process tree current while the server runs.

        Often at first, while wrappers start the real server, then twice a second.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        while True:
            await asyncio.to_thread(self._job.refresh)
            await asyncio.sleep(0.05 if loop.time() - start < 3 else 0.5)

    async def close(self, grace: float = 2.0) -> int | None:
        """Shut the server down; returns its exit code. Safe to call twice."""
        process = self._process
        if process is None:
            return None
        self._closing = True
        try:
            if process.returncode is None:
                if process.stdin is not None and not process.stdin.is_closing():
                    process.stdin.close()
                for force in (None, False, True):
                    if force is not None:
                        terminate_tree(process, force=force, job=self._job)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), grace)
                        break
            await process.wait()
        finally:
            # Kill the whole tree even if we were cancelled (Ctrl+C) half-way
            # through, or the direct child exited but left descendants behind.
            terminate_tree(process, force=True, job=self._job)
            if self._tracker is not None:  # pragma: no cover - Windows only
                self._tracker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._tracker
            self._job.close()
            await self._stop_readers(grace)
        return process.returncode

    async def _stop_readers(self, grace: float) -> None:
        """Let readers drain what is buffered; never wait on them forever.

        A descendant that escaped the kill can hold the pipes open, so EOF
        might never come.
        """
        pending = [task for task in self._readers if not task.done()]
        if pending:
            _, still_running = await asyncio.wait(pending, timeout=grace)
            for task in still_running:  # pragma: no cover - needs an unkillable orphan
                task.cancel()
        for task in self._readers:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    # ------------------------------------------------------------ sending

    async def send(self, data: bytes) -> None:
        """Write raw bytes to the server's stdin. Records exactly ``data``."""
        process = self._require_process()
        if process.stdin is None or process.stdin.is_closing():
            raise TransportClosed("server stdin is closed")
        self._recorder.stdio("stdin", data)
        try:
            process.stdin.write(data)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TransportClosed(self._died_message("stdin closed")) from exc

    async def send_line(self, data: bytes) -> None:
        """Send one message: ``data`` followed by a newline."""
        await self.send(data + b"\n")

    # ------------------------------------------------------------ receiving

    async def receive(self, timeout: float) -> bytes:
        """Next stdout line without its trailing newline."""
        try:
            item = await asyncio.wait_for(self._lines.get(), timeout)
        except TimeoutError as exc:
            raise TransportTimeout(f"no output within {timeout:g}s") from exc
        if isinstance(item, _Eof):
            self._lines.put_nowait(item)  # every later receive sees EOF too
            raise TransportClosed(self._died_message("server closed stdout"))
        if isinstance(item, Exception):
            raise item
        return item.removesuffix(b"\n")

    @property
    def stdout_count(self) -> int:
        """Number of stdout lines (and trailing fragments) recorded so far."""
        return self._stdout_count

    async def wait_for_stdout(self, count: int, timeout: float) -> bool:
        """Wait until at least ``count`` stdout lines arrived; False on timeout or EOF."""

        def ready() -> bool:
            return self._stdout_count >= count or self._stdout_done

        async with self._count_changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._count_changed.wait_for(ready), timeout)
        return self._stdout_count >= count

    async def wait_for_exit(self, timeout: float) -> int | None:
        process = self._require_process()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout)
        return process.returncode

    # ------------------------------------------------------------ internals

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RuntimeError("transport not started")
        return self._process

    def _died_message(self, what: str) -> str:
        tail = " | ".join(self.stderr_tail)
        return f"{what}; stderr: {tail}" if tail else what

    async def _bump(self) -> None:
        self._stdout_count += 1
        async with self._count_changed:
            self._count_changed.notify_all()

    async def _read_stdout(self, stream: asyncio.StreamReader) -> None:
        buffer = b""
        while True:
            chunk = await stream.read(READ_CHUNK)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                self._recorder.stdio("stdout", line + b"\n")
                self._lines.put_nowait(line + b"\n")
                await self._bump()
            if len(buffer) > self._max_line:
                self._recorder.stdio("stdout", buffer)
                self._lines.put_nowait(
                    LineTooLong(f"stdout line exceeded {self._max_line} bytes without a newline")
                )
                await self._bump()
                buffer = b""
        if buffer:
            # Server exited mid-line: keep the fragment, unterminated.
            self._recorder.stdio("stdout", buffer)
            self._lines.put_nowait(buffer)
            await self._bump()
        await self._finish_stdout()

    async def _finish_stdout(self) -> None:
        process = self._require_process()
        returncode = await process.wait()
        if not self._closing:
            self._recorder.exit(returncode)
        self._lines.put_nowait(_EOF)
        self._stdout_done = True
        async with self._count_changed:
            self._count_changed.notify_all()

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        stream_name: Literal["stderr"] = "stderr"
        buffer = b""
        while chunk := await stream.read(READ_CHUNK):
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                self._recorder.stdio(stream_name, line + b"\n")
                self.stderr_tail.append(line.decode("utf-8", "replace").rstrip("\r"))
            if len(buffer) > self._max_line:
                self._recorder.stdio(stream_name, buffer)
                buffer = b""
        if buffer:
            self._recorder.stdio(stream_name, buffer)
            self.stderr_tail.append(buffer.decode("utf-8", "replace"))
