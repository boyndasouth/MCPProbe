"""Collects frames as they cross the wire and masks secrets before saving."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from mcpprobe.transport.frames import (
    ExitFrame,
    Frame,
    Header,
    HttpErrorFrame,
    HttpRequestFrame,
    HttpResponseFrame,
    Recording,
    StdioFrame,
    Target,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

MASK = "<masked>"
MIN_SECRET_LENGTH = 4
"""Shorter values are not masked: masking "1" would garble every recording."""

_SENSITIVE_HEADER = re.compile(
    r"authorization|cookie|token|secret|password|api[-_]?key|credential", re.IGNORECASE
)


def is_sensitive_header(name: str) -> bool:
    return bool(_SENSITIVE_HEADER.search(name))


class Masker:
    """Replaces secret values with ``<masked>`` and flags the frames it touched.

    Secrets are every env value passed to the server and every value of a
    sensitive-looking header. Masking is applied when a recording is saved,
    so live traffic is never altered.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: set[str] = set()
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str) -> None:
        if len(secret) >= MIN_SECRET_LENGTH:
            self._secrets.add(secret)

    def add_headers(self, headers: Iterable[Header]) -> None:
        for name, value in headers:
            if is_sensitive_header(name):
                self.add(value)
                # "Bearer abc" -> also mask the bare token wherever it shows up
                self.add(value.partition(" ")[2])

    def mask_bytes(self, data: bytes) -> tuple[bytes, bool]:
        masked = data
        # Longest first so a secret containing another is replaced whole.
        for secret in sorted(self._secrets, key=lambda s: (-len(s), s)):
            masked = masked.replace(secret.encode("utf-8"), MASK.encode("ascii"))
        return masked, masked != data

    def mask_headers(self, headers: tuple[Header, ...]) -> tuple[tuple[Header, ...], bool]:
        out: list[Header] = []
        changed = False
        for name, value in headers:
            if is_sensitive_header(name):
                out.append((name, MASK))
                changed = True
            else:
                new_value, touched = self.mask_bytes(value.encode("utf-8"))
                out.append((name, new_value.decode("utf-8")))
                changed |= touched
        return tuple(out), changed

    def mask_frame(self, frame: Frame) -> Frame:
        if isinstance(frame, StdioFrame):
            data, touched = self.mask_bytes(frame.data)
            return frame.model_copy(update={"data": data, "masked": frame.masked or touched})
        if isinstance(frame, HttpRequestFrame | HttpResponseFrame):
            body, body_touched = self.mask_bytes(frame.body)
            headers, headers_touched = self.mask_headers(frame.headers)
            masked = frame.masked or body_touched or headers_touched
            return frame.model_copy(update={"body": body, "headers": headers, "masked": masked})
        return frame


class Recorder:
    """Append-only, ordered log of frames for one server connection."""

    def __init__(self) -> None:
        self._frames: list[Frame] = []
        self._exchanges = 0

    @property
    def frames(self) -> tuple[Frame, ...]:
        return tuple(self._frames)

    def _seq(self) -> int:
        return len(self._frames)

    def stdio(self, stream: Literal["stdin", "stdout", "stderr"], data: bytes) -> None:
        self._frames.append(StdioFrame(seq=self._seq(), stream=stream, data=data))

    def exit(self, returncode: int | None) -> None:
        self._frames.append(ExitFrame(seq=self._seq(), returncode=returncode))

    def new_exchange(self) -> int:
        exchange = self._exchanges
        self._exchanges += 1
        return exchange

    def http_request(
        self,
        exchange: int,
        method: str,
        url: str,
        headers: tuple[Header, ...],
        body: bytes,
        stop_after_events: int | None,
    ) -> None:
        self._frames.append(
            HttpRequestFrame(
                seq=self._seq(),
                exchange=exchange,
                method=method,
                url=url,
                headers=headers,
                body=body,
                stop_after_events=stop_after_events,
            )
        )

    def http_response(
        self,
        exchange: int,
        status: int,
        http_version: str,
        headers: tuple[Header, ...],
        body: bytes,
        *,
        complete: bool,
    ) -> None:
        self._frames.append(
            HttpResponseFrame(
                seq=self._seq(),
                exchange=exchange,
                status=status,
                http_version=http_version,
                headers=headers,
                body=body,
                complete=complete,
            )
        )

    def http_error(self, exchange: int, error: str) -> None:
        self._frames.append(HttpErrorFrame(seq=self._seq(), exchange=exchange, error=error))

    def recording(self, target: Target, masker: Masker | None = None) -> Recording:
        frames = self._frames if masker is None else [masker.mask_frame(f) for f in self._frames]
        return Recording(target=target, frames=tuple(frames))
