from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.support.http_server import serve

if TYPE_CHECKING:
    from collections.abc import Iterator

SUPPORT = Path(__file__).parent / "support"


@pytest.fixture
def stdio_argv() -> list[str]:
    return [sys.executable, "-u", str(SUPPORT / "stdio_server.py")]


@pytest.fixture
def http_url() -> Iterator[str]:
    server, url = serve()
    yield url
    server.shutdown()
    server.server_close()
