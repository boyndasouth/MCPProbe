"""Incremental Server-Sent Events parser (WHATWG HTML, "event stream" parsing).

Fed raw bytes in arbitrary chunks; yields complete events. Kept strict to the
standard so MCPProbe can tell a server bug from a parser bug: comment lines
are ignored, ``\\r\\n``, ``\\r`` and ``\\n`` all end a line, and an event
without a trailing blank line at end-of-stream is discarded.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str = "message"
    data: str = ""
    id: str | None = None
    """The stream's last event ID when this event was dispatched."""
    retry: int | None = None


class SseParser:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._at_start = True
        self._data: list[str] = []
        self._event = ""
        self._last_id: str | None = None
        self._retry: int | None = None
        self.comments = 0
        """Number of comment lines seen (keep-alives)."""

    def feed(self, chunk: bytes) -> list[SseEvent]:
        return self._process(self._decoder.decode(chunk), final=False)

    def close(self) -> list[SseEvent]:
        """Flush at end of stream. An unterminated event is dropped per the standard."""
        events = self._process(self._decoder.decode(b"", final=True), final=True)
        self._data.clear()
        self._event = ""
        return events

    def _process(self, text: str, *, final: bool) -> list[SseEvent]:
        text = self._buffer + text
        if self._at_start and text:
            text = text.removeprefix("﻿")
            self._at_start = False
        events: list[SseEvent] = []
        start = i = 0
        while i < len(text):
            char = text[i]
            if char == "\n":
                end, i = i, i + 1
            elif char == "\r":
                if i + 1 == len(text) and not final:
                    break  # may be the first half of \r\n; wait for more input
                end = i
                i += 2 if text[i + 1 : i + 2] == "\n" else 1
            else:
                i += 1
                continue
            event = self._line(text[start:end])
            if event is not None:
                events.append(event)
            start = i
        self._buffer = text[start:]
        return events

    def _line(self, line: str) -> SseEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            self.comments += 1
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id":
            if "\0" not in value:
                self._last_id = value
        elif field == "retry" and value.isascii() and value.isdigit():
            self._retry = int(value)
        return None

    def _dispatch(self) -> SseEvent | None:
        if not self._data:
            self._event = ""
            return None
        event = SseEvent(
            event=self._event or "message",
            data="\n".join(self._data),
            id=self._last_id,
            retry=self._retry,
        )
        self._data.clear()
        self._event = ""
        return event


def parse_sse(body: bytes) -> list[SseEvent]:
    """Parse a complete event-stream body."""
    parser = SseParser()
    return parser.feed(body) + parser.close()
