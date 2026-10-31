"""The reference fixture server (fixtures/servers/reference.py), loaded for tests."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any

from mcpprobe.engine.connection import StdioServer

REFERENCE = Path(__file__).parents[2] / "fixtures" / "servers" / "reference.py"


def _load_reference() -> Any:
    spec = importlib.util.spec_from_file_location("mcpprobe_reference_fixture", REFERENCE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reference = _load_reference()


class HttpFixture:
    def __init__(self, breaks: set[str], era: str = "modern", token: str | None = None) -> None:
        self.httpd = reference.make_http_server(reference.Server(breaks, era, token))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def stdio_spec(breaks: tuple[str, ...] = (), era: str = "modern") -> StdioServer:
    flags = [arg for check in breaks for arg in ("--break", check)]
    return StdioServer((sys.executable, "-u", str(REFERENCE), "--era", era, *flags))
