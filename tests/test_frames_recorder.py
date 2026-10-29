from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mcpprobe.transport.frames import (
    HttpRequestFrame,
    HttpTarget,
    Recording,
    StdioFrame,
    StdioTarget,
)
from mcpprobe.transport.recorder import MASK, Masker, Recorder, is_sensitive_header


@pytest.mark.parametrize("data", [b'{"a":1}\n', b"caf\xc3\xa9\n", b"\xff\xfe raw\n", b""])
def test_bytes_round_trip_exactly(data: bytes) -> None:
    recorder = Recorder()
    recorder.stdio("stdout", data)
    recording = recorder.recording(StdioTarget(argv=("x",)))
    loaded = Recording.loads(recording.dumps())
    assert loaded == recording
    assert loaded.stdio_frames("stdout")[0].data == data


def test_readable_text_and_base64_fallback() -> None:
    recorder = Recorder()
    recorder.stdio("stdin", b"hello\n")
    recorder.stdio("stdout", b"\xff")
    raw = json.loads(recorder.recording(StdioTarget(argv=("x",))).dumps())
    assert raw["frames"][0]["data"] == {"utf8": "hello\n"}
    assert raw["frames"][1]["data"] == {"base64": "/w=="}


def test_dumps_is_deterministic() -> None:
    def build() -> str:
        recorder = Recorder()
        recorder.stdio("stdin", b"a")
        exchange = recorder.new_exchange()
        recorder.http_request(exchange, "POST", "http://x", (("A", "b"),), b"{}", None)
        recorder.http_response(exchange, 200, "HTTP/1.1", (), b"ok", complete=True)
        recorder.http_error(recorder.new_exchange(), "timeout")
        recorder.exit(0)
        return recorder.recording(HttpTarget(url="http://x")).dumps()

    assert build() == build()


@pytest.mark.parametrize(
    "bad",
    [{"utf8": 1}, {"base64": "!!"}, {"hex": "00"}, "plain string"],
)
def test_rejects_malformed_bytes(bad: object) -> None:
    with pytest.raises(ValidationError):
        StdioFrame.model_validate({"seq": 0, "stream": "stdin", "data": bad})


def test_rejects_unknown_format_version() -> None:
    doc = json.loads(Recording(target=StdioTarget(argv=("x",))).dumps())
    doc["version"] = 2
    with pytest.raises(ValidationError):
        Recording.loads(json.dumps(doc))


@pytest.mark.parametrize(
    ("name", "sensitive"),
    [
        ("Authorization", True),
        ("X-Api-Key", True),
        ("Cookie", True),
        ("Mcp-Method", False),
        ("Content-Type", False),
    ],
)
def test_sensitive_header_detection(name: str, sensitive: bool) -> None:
    assert is_sensitive_header(name) is sensitive


def test_masker_masks_env_values_and_header_tokens() -> None:
    masker = Masker(["supersecret", "abc"])  # "abc" is too short to mask
    masker.add_headers([("Authorization", "Bearer tok-12345"), ("Accept", "a")])
    recorder = Recorder()
    recorder.stdio("stdout", b'{"k":"supersecret","t":"tok-12345","abc":1}\n')
    exchange = recorder.new_exchange()
    recorder.http_request(
        exchange, "POST", "u", (("Authorization", "Bearer tok-12345"), ("X", "y")), b"{}", None
    )
    recording = recorder.recording(StdioTarget(argv=("x",)), masker)
    stdout, request = recording.frames
    assert isinstance(stdout, StdioFrame)
    assert stdout.data == b'{"k":"<masked>","t":"<masked>","abc":1}\n'
    assert stdout.masked
    assert isinstance(request, HttpRequestFrame)
    assert request.headers == (("Authorization", MASK), ("X", "y"))
    assert request.masked


def test_masker_leaves_clean_frames_unflagged() -> None:
    recorder = Recorder()
    recorder.stdio("stdout", b"nothing here\n")
    recorder.exit(0)
    recording = recorder.recording(StdioTarget(argv=("x",)), Masker(["secretvalue"]))
    assert not recording.stdio_frames("stdout")[0].masked
