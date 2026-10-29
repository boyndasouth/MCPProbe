"""Deterministic HTTP test server for transport tests. Not an MCP server."""

from __future__ import annotations

import itertools
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

TOKEN = "Bearer s3cret-token-value"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sessions: ClassVar[set[str]] = set()
    counter: ClassVar[itertools.count[int]] = itertools.count(1)

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(405, b"")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Authorization") not in (None, TOKEN):
            self._send(401, b"", {"WWW-Authenticate": "Bearer"})
            return
        msg = json.loads(body)
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            self._send(202, b"")
            return
        if method == "initialize":
            new_session = f"session-{next(self.counter)}-{time.monotonic_ns()}"
            self.sessions.add(new_session)
            reply = {"jsonrpc": "2.0", "id": mid, "result": {"ok": True}}
            self._send(
                200,
                json.dumps(reply).encode(),
                {"Content-Type": "application/json", "Mcp-Session-Id": new_session},
            )
            return
        session: str | None = self.headers.get("Mcp-Session-Id")
        if session is not None and session not in self.sessions:
            self._send(404, b"")
            return
        if method == "sleep":
            time.sleep(float(msg["params"]["seconds"]))
        if method in ("stream", "endless"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": keep-alive\r\n\r\n")
            for n in itertools.count(1):
                if method == "stream" and n > 3:
                    break
                note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"n": n}}
                try:
                    self.wfile.write(f"id: {n}\r\ndata: {json.dumps(note)}\r\n\r\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if method == "endless":
                    time.sleep(0.01)
            final = {"jsonrpc": "2.0", "id": mid, "result": {"done": True}}
            self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
            self.close_connection = True
            return
        reply = {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "method": method,
                "mcp_method_header": self.headers.get("Mcp-Method"),
                "mcp_name_header": self.headers.get("Mcp-Name"),
            },
        }
        self._send(
            200,
            json.dumps(reply, sort_keys=True).encode(),
            {"Content-Type": "application/json", "Date": time.strftime("%H:%M:%S")},
        )


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/mcp"
