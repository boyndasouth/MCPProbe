from __future__ import annotations

import pytest

from mcpprobe.transport.sse import SseEvent, SseParser, parse_sse


def test_basic_events_and_defaults() -> None:
    events = parse_sse(b"data: one\n\nevent: custom\ndata: two\n\n")
    assert events == [SseEvent(data="one"), SseEvent(event="custom", data="two")]


def test_multiline_data_and_optional_space() -> None:
    assert parse_sse(b"data:a\ndata:  b\n\n") == [SseEvent(data="a\n b")]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_all_line_endings(newline: bytes) -> None:
    body = b"data: x" + newline + newline
    assert parse_sse(body) == [SseEvent(data="x")]


def test_every_chunk_split_gives_same_events() -> None:
    body = "\ufeff: hi\r\nid: 7\r\ndata: héllo\r\n\r\ndata: 2\r\n\r\n".encode()
    expected = parse_sse(body)
    assert expected == [SseEvent(data="héllo", id="7"), SseEvent(data="2", id="7")]
    for cut in range(1, len(body)):
        parser = SseParser()
        got = parser.feed(body[:cut]) + parser.feed(body[cut:]) + parser.close()
        assert got == expected, cut


def test_comments_are_counted_not_dispatched() -> None:
    parser = SseParser()
    assert parser.feed(b": keep-alive\n\n:\n\n") == []
    assert parser.comments == 2


def test_unterminated_event_is_dropped_at_eof() -> None:
    assert parse_sse(b"data: complete\n\ndata: cut off") == [SseEvent(data="complete")]


def test_id_with_null_ignored_and_retry_parsed() -> None:
    events = parse_sse(b"id: 1\nretry: 250\ndata: a\n\nid: bad\x00\nretry: x\ndata: b\n\n")
    assert events == [SseEvent(data="a", id="1", retry=250), SseEvent(data="b", id="1", retry=250)]


def test_field_without_colon_and_unknown_fields() -> None:
    assert parse_sse(b"data\nfoo: bar\n\n") == [SseEvent(data="")]


def test_empty_event_resets_type() -> None:
    assert parse_sse(b"event: x\n\ndata: y\n\n") == [SseEvent(data="y")]
