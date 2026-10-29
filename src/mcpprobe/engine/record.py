"""Send a scripted list of messages to a server and record the session.

This is the developer-facing way to exercise the wire client against a real
server before any suite exists, and the source of recordings for replay
tests. Messages are sent byte-for-byte as given: one per line on stdio, one
POST body each on HTTP.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcpprobe.protocol.http_headers import request_metadata_headers
from mcpprobe.transport.errors import TransportClosed, TransportError, TransportTimeout
from mcpprobe.transport.frames import HttpTarget, Recording, StdioTarget
from mcpprobe.transport.http import HttpTransport
from mcpprobe.transport.recorder import Masker, Recorder
from mcpprobe.transport.stdio import StdioTransport

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcpprobe.transport.frames import Header

ACCEPT = "application/json, text/event-stream"
LEGACY_SESSION_HEADER = "Mcp-Session-Id"


@dataclass
class RecordResult:
    recording: Recording
    sent: int = 0
    answered: int = 0
    problems: list[str] = field(default_factory=list)


def request_id(message: bytes) -> tuple[bool, Any]:
    """(True, id) if ``message`` is a JSON-RPC request we should wait for."""
    try:
        parsed = json.loads(message)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None
    if isinstance(parsed, dict) and "id" in parsed and "method" in parsed:
        return True, parsed["id"]
    return False, None


def response_id(line: bytes) -> tuple[bool, Any]:
    try:
        parsed = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None
    if isinstance(parsed, dict) and "id" in parsed and "method" not in parsed:
        return True, parsed["id"]
    return False, None


async def _await_response(transport: StdioTransport, wanted: Any, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (remaining := deadline - loop.time()) > 0:
        try:
            line = await transport.receive(remaining)
        except TransportTimeout:
            return False
        is_response, got = response_id(line)
        if is_response and got == wanted:
            return True
    return False


async def record_stdio(
    argv: Sequence[str],
    messages: Sequence[bytes],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 10.0,
    settle: float = 0.2,
) -> RecordResult:
    env = dict(env or {})
    recorder = Recorder()
    transport = StdioTransport(argv, recorder=recorder, env=env, cwd=cwd)
    result_problems: list[str] = []
    sent = answered = 0
    await transport.start()
    try:
        for message in messages:
            await transport.send_line(message)
            sent += 1
            wait, wanted = request_id(message)
            if not wait:
                continue
            if await _await_response(transport, wanted, timeout):
                answered += 1
            else:
                result_problems.append(f"message {sent}: no response with id {wanted!r}")
        # Let trailing notifications land so the recording is complete.
        await transport.wait_for_stdout(transport.stdout_count + 1, settle)
    except TransportClosed as exc:
        result_problems.append(str(exc))
    finally:
        await transport.close()
    target = StdioTarget(argv=tuple(argv), cwd=cwd, env_keys=tuple(sorted(env)))
    recording = recorder.recording(target, Masker(env.values()))
    return RecordResult(recording, sent, answered, result_problems)


async def record_http(
    url: str,
    messages: Sequence[bytes],
    *,
    headers: Sequence[Header] = (),
    derive_headers: bool = True,
    timeout: float = 10.0,
) -> RecordResult:
    recorder = Recorder()
    problems: list[str] = []
    sent = answered = 0
    session: str | None = None
    async with HttpTransport(url, recorder=recorder, headers=headers, timeout=timeout) as transport:
        for message in messages:
            extra: list[Header] = [("Content-Type", "application/json"), ("Accept", ACCEPT)]
            if derive_headers:
                extra.extend(request_metadata_headers(message))
            if session is not None:
                extra.append((LEGACY_SESSION_HEADER, session))
            sent += 1
            try:
                exchange = await transport.request("POST", message, extra)
            except TransportError as exc:
                problems.append(f"message {sent}: {exc}")
                if isinstance(exc, TransportClosed):
                    break
                continue
            session = exchange.header(LEGACY_SESSION_HEADER) or session
            wait, _ = request_id(message)
            if wait and exchange.status == 200:
                answered += 1
            elif wait:
                problems.append(f"message {sent}: HTTP {exchange.status}")
    masker = Masker()
    masker.add_headers(headers)
    recording = recorder.recording(HttpTarget(url=url), masker)
    return RecordResult(recording, sent, answered, problems)
