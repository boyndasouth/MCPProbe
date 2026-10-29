"""Replay a recording against a fresh server and compare what comes back.

The replay re-sends every recorded client frame byte for byte, in the
original order, and preserves causality: before each send it waits until the
server has produced as many output lines (stdio) as it had at that point in
the original session. Responses are then compared per channel.

Two substitutions are allowed, and nothing else:

- masked secrets (sensitive header values, env values) come from the caller,
  since recordings never store them;
- a legacy ``Mcp-Session-Id`` minted by the server in the original session
  is swapped for the one the server mints this time.

stderr is recorded but never compared: it is free-form logging.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mcpprobe.transport.errors import TransportClosed, TransportError
from mcpprobe.transport.frames import (
    ExitFrame,
    HttpErrorFrame,
    HttpRequestFrame,
    HttpResponseFrame,
    HttpTarget,
    Recording,
    StdioFrame,
    StdioTarget,
)
from mcpprobe.transport.http import HttpTransport
from mcpprobe.transport.recorder import MASK, Masker, Recorder, is_sensitive_header
from mcpprobe.transport.sse import parse_sse
from mcpprobe.transport.stdio import StdioTransport

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcpprobe.transport.frames import Header

SESSION_HEADER = "mcp-session-id"
# Response headers that legitimately differ between runs.
VOLATILE_HEADERS = frozenset(
    {
        "date",
        "age",
        "expires",
        "last-modified",
        "etag",
        "server",
        "via",
        "set-cookie",
        "x-request-id",
        "request-id",
        "cf-ray",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        SESSION_HEADER,
    }
)
PREVIEW = 120


class ReplayError(Exception):
    """The recording cannot be replayed as given (missing secret, bad file)."""


@dataclass(frozen=True)
class ReplayResult:
    original: Recording
    replayed: Recording
    differences: tuple[str, ...] = field(default=())

    @property
    def matched(self) -> bool:
        return not self.differences

    @property
    def silent(self) -> bool:
        """The client sent something but the server never answered, in the original.

        A replay of such a recording "matches" trivially (nothing == nothing),
        which says nothing about the server; callers should say so.
        """
        frames = self.original.frames
        sent = any(isinstance(f, HttpRequestFrame) for f in frames) or bool(
            self.original.stdio_frames("stdin")
        )
        answered = any(isinstance(f, HttpResponseFrame) for f in frames) or bool(
            self.original.stdio_frames("stdout")
        )
        return sent and not answered


def _preview(data: bytes) -> str:
    text = data.decode("utf-8", "replace")
    return text if len(text) <= PREVIEW else text[:PREVIEW] + "..."


def _compare_sequences(label: str, old: Sequence[bytes], new: Sequence[bytes]) -> list[str]:
    diffs: list[str] = []
    for index, (a, b) in enumerate(zip(old, new, strict=False), start=1):
        if a != b:
            diffs.append(f"{label} #{index} differs:\n  was: {_preview(a)}\n  now: {_preview(b)}")
    if len(old) != len(new):
        diffs.append(f"{label}: {len(old)} frames originally, {len(new)} now")
    return diffs


# ----------------------------------------------------------------- stdio


async def _replay_stdio(
    recording: Recording,
    target: StdioTarget,
    env: Mapping[str, str],
    timeout: float,
) -> ReplayResult:
    if any(f.masked for f in recording.stdio_frames("stdin")):
        raise ReplayError("a sent frame contained a secret and was masked; it cannot be re-sent")
    missing = [key for key in target.env_keys if key not in env and key not in os.environ]
    if missing:
        raise ReplayError(f"set these environment variables to replay: {', '.join(missing)}")
    server_env = {key: env.get(key, os.environ.get(key, "")) for key in target.env_keys}

    recorder = Recorder()
    transport = StdioTransport(target.argv, recorder=recorder, env=server_env, cwd=target.cwd)
    expected_out = 0
    exited = any(isinstance(f, ExitFrame) for f in recording.frames)
    problems: list[str] = []
    await transport.start()
    try:
        for frame in recording.frames:
            if isinstance(frame, StdioFrame) and frame.stream == "stdout":
                expected_out += 1
            elif isinstance(frame, StdioFrame) and frame.stream == "stdin":
                await transport.wait_for_stdout(expected_out, timeout)
                await transport.send(frame.data)
        await transport.wait_for_stdout(expected_out, timeout)
        if exited:
            await transport.wait_for_exit(timeout)
    except TransportClosed as exc:
        problems.append(f"connection lost during replay: {exc}")
    finally:
        await transport.close()

    replayed = recorder.recording(target, Masker(server_env.values()))
    diffs = problems + _compare_sequences(
        "stdout",
        [f.data for f in recording.stdio_frames("stdout")],
        [f.data for f in replayed.stdio_frames("stdout")],
    )
    old_exit = [f.returncode for f in recording.frames if isinstance(f, ExitFrame)]
    new_exit = [f.returncode for f in replayed.frames if isinstance(f, ExitFrame)]
    if old_exit != new_exit:
        diffs.append(f"server exit: was {old_exit or 'none'}, now {new_exit or 'none'}")
    return ReplayResult(recording, replayed, tuple(diffs))


# ------------------------------------------------------------------ http


def _fill_secrets(headers: tuple[Header, ...], provided: Mapping[str, str]) -> list[Header]:
    filled: list[Header] = []
    for name, value in headers:
        if value == MASK and is_sensitive_header(name):
            real = provided.get(name.lower())
            if real is None:
                raise ReplayError(f"header {name!r} was masked; pass it with --header to replay")
            value = real
        filled.append((name, value))
    return filled


def _comparable_headers(headers: tuple[Header, ...]) -> list[Header]:
    return sorted((k.lower(), v) for k, v in headers if k.lower() not in VOLATILE_HEADERS)


def _compare_responses(
    index: int, old: HttpResponseFrame | None, new: HttpResponseFrame | None, stop: int | None
) -> list[str]:
    label = f"exchange #{index}"
    if old is None or new is None:
        if old is new:
            return []
        return [f"{label}: response {'missing' if new is None else 'appeared'} on replay"]
    diffs: list[str] = []
    if old.status != new.status:
        diffs.append(f"{label}: status was {old.status}, now {new.status}")
    if _comparable_headers(old.headers) != _comparable_headers(new.headers):
        diffs.append(f"{label}: response headers differ")
    if stop is not None:
        # Chunk boundaries vary, so an early-closed stream compares by event.
        old_events = parse_sse(old.body)[:stop]
        new_events = parse_sse(new.body)[:stop]
        if old_events != new_events:
            diffs.append(f"{label}: first {stop} SSE events differ")
    elif old.body != new.body:
        diffs.append(
            f"{label}: body differs:\n  was: {_preview(old.body)}\n  now: {_preview(new.body)}"
        )
    return diffs


async def _replay_http(
    recording: Recording,
    target: HttpTarget,
    headers: Sequence[Header],
    timeout: float,
) -> ReplayResult:
    provided = {name.lower(): value for name, value in headers}
    requests = [f for f in recording.frames if isinstance(f, HttpRequestFrame)]
    if any(f.masked and MASK.encode() in f.body for f in requests):
        raise ReplayError("a request body contained a secret and was masked; cannot re-send it")
    # Fail before sending anything if a secret is missing.
    for frame in requests:
        _fill_secrets(frame.headers, provided)

    old_responses = {f.exchange: f for f in recording.frames if isinstance(f, HttpResponseFrame)}
    recorder = Recorder()
    session_map: dict[str, str] = {}
    problems: list[str] = []
    async with HttpTransport(target.url, recorder=recorder, timeout=timeout) as transport:
        for frame in requests:
            send_headers = [
                (k, session_map.get(v, v) if k.lower() == SESSION_HEADER else v)
                for k, v in _fill_secrets(frame.headers, provided)
            ]
            try:
                exchange = await transport.request(
                    frame.method,
                    frame.body,
                    send_headers,
                    url=frame.url,
                    stop_after_events=frame.stop_after_events,
                )
            except TransportError as exc:
                problems.append(f"exchange #{frame.exchange + 1}: {exc}")
                continue
            old = old_responses.get(frame.exchange)
            old_session = _header(old.headers, SESSION_HEADER) if old else None
            new_session = exchange.header(SESSION_HEADER)
            if old_session and new_session:
                session_map[old_session] = new_session

    masker = Masker()
    masker.add_headers(headers)
    replayed = recorder.recording(target, masker)
    new_responses = {f.exchange: f for f in replayed.frames if isinstance(f, HttpResponseFrame)}
    old_errors = {f.exchange for f in recording.frames if isinstance(f, HttpErrorFrame)}
    new_errors = {f.exchange for f in replayed.frames if isinstance(f, HttpErrorFrame)}
    diffs = list(problems) if old_errors != new_errors else []
    for frame in requests:
        diffs += _compare_responses(
            frame.exchange + 1,
            old_responses.get(frame.exchange),
            new_responses.get(frame.exchange),
            frame.stop_after_events,
        )
    return ReplayResult(recording, replayed, tuple(diffs))


def _header(headers: tuple[Header, ...], name: str) -> str | None:
    return next((v for k, v in headers if k.lower() == name), None)


async def replay(
    recording: Recording,
    *,
    headers: Sequence[Header] = (),
    env: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> ReplayResult:
    target = recording.target
    if isinstance(target, StdioTarget):
        return await _replay_stdio(recording, target, dict(env or {}), timeout)
    return await _replay_http(recording, target, headers, timeout)
