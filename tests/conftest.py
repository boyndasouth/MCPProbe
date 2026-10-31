from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.support.http_server import serve
from tests.support.reference import HttpFixture

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

SUPPORT = Path(__file__).parent / "support"


@pytest.fixture
def stdio_argv() -> list[str]:
    return [sys.executable, "-u", str(SUPPORT / "stdio_server.py")]


@pytest.fixture
def modern_stdio_argv(stdio_argv: list[str]) -> list[str]:
    """The echo server, answering server/discover so it counts as modern."""
    return [*stdio_argv, "--modern"]


@pytest.fixture
def http_url() -> Iterator[str]:
    server, url = serve()
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def http_fixture() -> Iterator[Any]:
    fixtures: list[HttpFixture] = []

    def start(breaks: set[str] | None = None, era: str = "modern") -> HttpFixture:
        fixture = HttpFixture(breaks or set(), era)
        fixtures.append(fixture)
        return fixture

    yield start
    for fixture in fixtures:
        fixture.close()
