"""The recording format: every byte that crossed the wire, in order.

A recording is what makes a failure replayable. It stores raw bytes, never
parsed messages, so a malformed line or a broken SSE stream is kept exactly as
the server sent it. Bytes are written as ``{"utf8": "..."}`` when they are
valid UTF-8 (readable in a diff) and ``{"base64": "..."}`` otherwise; both
round-trip exactly.

Nothing time-dependent is stored: no timestamps, no durations.
"""

from __future__ import annotations

import base64
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

RECORDING_FORMAT = "mcpprobe.recording"
RECORDING_VERSION = 1

Header = tuple[str, str]


def _bytes_to_json(data: bytes) -> dict[str, str]:
    try:
        return {"utf8": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(data).decode("ascii")}


def _bytes_from_json(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, dict) and len(value) == 1:
        ((key, text),) = value.items()
        if key == "utf8" and isinstance(text, str):
            return text.encode("utf-8")
        if key == "base64" and isinstance(text, str):
            return base64.b64decode(text, validate=True)
    raise ValueError('expected {"utf8": str} or {"base64": str}')


WireBytes = Annotated[
    bytes,
    BeforeValidator(_bytes_from_json),
    PlainSerializer(_bytes_to_json, return_type=dict[str, str], when_used="json"),
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------- frames


class StdioFrame(_Model):
    """Bytes written to stdin, or one line read from stdout or stderr.

    Read lines keep their trailing ``\\n``. A final fragment without one (the
    server exited mid-line) is stored as-is, so it is visible in the data.
    """

    kind: Literal["stdio"] = "stdio"
    seq: int
    stream: Literal["stdin", "stdout", "stderr"]
    data: WireBytes
    masked: bool = False


class ExitFrame(_Model):
    """The server process exited on its own (not because we closed it)."""

    kind: Literal["exit"] = "exit"
    seq: int
    returncode: int | None


class HttpRequestFrame(_Model):
    kind: Literal["http_request"] = "http_request"
    seq: int
    exchange: int
    method: str
    url: str
    headers: tuple[Header, ...]
    """Headers set by the caller, in order. Client defaults are fixed (see http.py)."""
    body: WireBytes
    stop_after_events: int | None = None
    """If set, the client closed the response after this many SSE events."""
    masked: bool = False


class HttpResponseFrame(_Model):
    kind: Literal["http_response"] = "http_response"
    seq: int
    exchange: int
    status: int
    http_version: str
    headers: tuple[Header, ...]
    body: WireBytes
    complete: bool
    """False if the body was cut short (client closed early, size cap, drop)."""
    masked: bool = False


class HttpErrorFrame(_Model):
    """The exchange failed below HTTP: refused, reset, timed out."""

    kind: Literal["http_error"] = "http_error"
    seq: int
    exchange: int
    error: str


Frame = Annotated[
    StdioFrame | ExitFrame | HttpRequestFrame | HttpResponseFrame | HttpErrorFrame,
    Field(discriminator="kind"),
]


# -------------------------------------------------------------------- targets


class StdioTarget(_Model):
    transport: Literal["stdio"] = "stdio"
    argv: tuple[str, ...]
    cwd: str | None = None
    env_keys: tuple[str, ...] = ()
    """Names of extra environment variables the server got. Values are never stored."""


class HttpTarget(_Model):
    transport: Literal["http"] = "http"
    url: str


Target = Annotated[StdioTarget | HttpTarget, Field(discriminator="transport")]


class Recording(_Model):
    format: Literal["mcpprobe.recording"] = "mcpprobe.recording"
    version: Literal[1] = 1
    target: Target
    frames: tuple[Frame, ...] = ()

    def dumps(self) -> str:
        return self.model_dump_json(indent=2) + "\n"

    @classmethod
    def loads(cls, text: str | bytes) -> Recording:
        return cls.model_validate_json(text)

    def stdio_frames(self, stream: Literal["stdin", "stdout", "stderr"]) -> tuple[StdioFrame, ...]:
        return tuple(f for f in self.frames if isinstance(f, StdioFrame) and f.stream == stream)
