"""Reference MCP server (2026-07-28 only) for MCPProbe's own tests.

Standard library only, written against the raw wire protocol so it can break
the protocol on purpose. Run with no flags it passes every implemented check;
``--break CONF-NNN`` injects exactly the violation that check looks for, which
gives each check its failing ("bad") and passing ("good") fixture.

    python reference.py                          # stdio, conformant
    python reference.py --break CONF-004         # stdio, results lack resultType
    python reference.py --http 0                 # HTTP on a free port (printed on stderr)

Breakable checks: see BREAKS below.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

VERSION = "2026-07-28"
SUPPORTED = [VERSION]
PV = "io.modelcontextprotocol/protocolVersion"
CAPS = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO = {"name": "mcpprobe-reference", "version": "1.0.0"}

BREAKS = {
    "CONF-001": "server/discover is not implemented",
    "CONF-002": "requests missing required _meta fields are accepted",
    "CONF-003": "unsupported versions get -32602 without data.supported",
    "CONF-004": "results omit resultType",
    "CONF-005": "integer request ids are echoed back as strings",
    "CONF-006": "HTTP headers are not checked against the body",
    "CONF-007": "the Origin header is not validated",
    "CONF-012": "tools/list order changes on every call",
    "CONF-015": "a startup banner is printed on stdout",
    "CONF-016": "the deprecated logging capability is declared",
    "CONF-017": "results carry no serverInfo",
    "CONF-018": "cacheable results carry no ttlMs/cacheScope",
    "CONF-020": "the first protocol version seen is reused for every later request",
    "CONF-009": "ask_name sends an elicitation input request the client never declared",
    "CONF-010": "ask_name's input_required result has neither inputRequests nor requestState",
    "CONF-011": "weather returns structuredContent that does not match its outputSchema",
}

TOOLS = [
    {
        "name": "echo",
        "description": "Echoes back the input string",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_time",
        "description": "Returns a fixed time, for deterministic tests",
        "inputSchema": {"type": "object", "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "ask_name",
        "description": "Asks the user for their name through elicitation, then greets them",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "weather",
        "description": "Returns fixed weather data as structured content",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string", "enum": ["Austin", "Oslo"]}},
            "required": ["city"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "celsius": {"type": "number"}},
            "required": ["city", "celsius"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        # Trap: MCPProbe must never call this without --allow-destructive.
        "name": "delete_everything",
        "description": "Destructive. Calling it shuts the server down.",
        "inputSchema": {"type": "object"},
        "annotations": {"destructiveHint": True},
    },
]


class Rpc(Exception):
    def __init__(self, code: int, message: str, data: Any = None, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.data, self.status = code, message, data, status


class Server:
    def __init__(self, breaks: set[str]) -> None:
        self.breaks = breaks
        self.list_calls = 0
        self.remembered_version: str | None = None
        self.lock = threading.Lock()

    # ------------------------------------------------------------ results

    def _result(self, body: dict[str, Any], *, cacheable: bool = False) -> dict[str, Any]:
        if "CONF-004" not in self.breaks:
            body["resultType"] = "complete"
        if cacheable and "CONF-018" not in self.breaks:
            body["ttlMs"] = 60000
            body["cacheScope"] = "public"
        if "CONF-017" not in self.breaks:
            body["_meta"] = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}
        return body

    def _check_meta(self, method: str, params: dict[str, Any]) -> None:
        meta = params.get("_meta")
        meta = meta if isinstance(meta, dict) else {}
        if "CONF-002" not in self.breaks:
            for key in (PV, CAPS):
                if key not in meta:
                    raise Rpc(-32602, f"missing required _meta field {key}")
        version = meta.get(PV, VERSION)
        if "CONF-020" in self.breaks:
            with self.lock:
                if self.remembered_version is None and version in SUPPORTED:
                    self.remembered_version = version
                version = self.remembered_version or version
        if version not in SUPPORTED:
            if "CONF-003" in self.breaks:
                raise Rpc(-32602, "bad version")
            raise Rpc(
                -32022,
                "Unsupported protocol version",
                {"supported": SUPPORTED, "requested": version},
            )

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "server/discover" and "CONF-001" not in self.breaks:
            caps: dict[str, Any] = {"tools": {}}
            if "CONF-016" in self.breaks:
                caps["logging"] = {}
            return self._result(
                {"supportedVersions": SUPPORTED, "capabilities": caps}, cacheable=True
            )
        if method == "tools/list":
            tools = list(TOOLS)
            with self.lock:
                self.list_calls += 1
                if "CONF-012" in self.breaks and self.list_calls % 2 == 0:
                    tools.reverse()
            return self._result({"tools": tools}, cacheable=True)
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                text = str(arguments.get("message", ""))
                return self._result({"content": [{"type": "text", "text": text}]})
            if name == "get_time":
                return self._result({"content": [{"type": "text", "text": "12:00"}]})
            if name == "ask_name":
                return self._ask_name(params)
            if name == "weather":
                city = str(arguments.get("city", "Austin"))
                data: dict[str, Any] = {"city": city, "celsius": 21.5}
                if "CONF-011" in self.breaks:
                    data["celsius"] = "warm"
                text = json.dumps(data)
                return self._result(
                    {"content": [{"type": "text", "text": text}], "structuredContent": data}
                )
            if name == "delete_everything":
                sys.stderr.write("delete_everything was called\n")
                sys.stderr.flush()
                raise SystemExit(99)
            raise Rpc(-32602, f"Unknown tool: {name}")
        raise Rpc(-32601, "Method not found", status=404)

    def _ask_name(self, params: dict[str, Any]) -> dict[str, Any]:
        """Multi round-trip request (mrtr#basic-workflow)."""
        answers = params.get("inputResponses")
        answer = answers.get("name") if isinstance(answers, dict) else None
        if isinstance(answer, dict) and answer.get("action") == "accept":
            content = answer.get("content") or {}
            text = f"Hello, {content.get('name', 'friend')}"
            return self._result({"content": [{"type": "text", "text": text}]})
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        caps = meta.get(CAPS) if isinstance(meta.get(CAPS), dict) else {}
        if "elicitation" not in caps and "CONF-009" not in self.breaks:
            raise Rpc(
                -32021,
                "Missing required client capability: elicitation",
                {"requiredCapabilities": {"elicitation": {}}},
            )
        if "CONF-010" in self.breaks:
            return {"resultType": "input_required"}
        return {
            "resultType": "input_required",
            "inputRequests": {
                "name": {
                    "method": "elicitation/create",
                    "params": {
                        "mode": "form",
                        "message": "What is your name?",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    },
                }
            },
            "requestState": "ask_name:v1",
        }

    # ------------------------------------------------------------ messages

    def handle(self, raw: bytes) -> tuple[int, dict[str, Any] | None]:
        """Process one message. Returns (HTTP status, response or None)."""
        try:
            message = json.loads(raw)
        except ValueError:
            return 400, _error(None, Rpc(-32700, "Parse error"))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return 400, _error(None, Rpc(-32600, "Invalid Request"))
        method = message.get("method")
        if not isinstance(method, str):
            return 400, _error(None, Rpc(-32600, "Invalid Request"))
        if "id" not in message:
            return 202, None  # notification: accept, never answer
        mid = message["id"]
        if mid is None or isinstance(mid, (bool, float, list, dict)):
            return 400, _error(None, Rpc(-32600, "Invalid Request: id must be a string or integer"))
        if "CONF-005" in self.breaks and isinstance(mid, int):
            mid = str(mid)
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        try:
            self._check_meta(method, params)
            result = self.dispatch(method, params)
        except Rpc as exc:
            return exc.status, _error(mid, exc)
        return 200, {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, exc: Rpc) -> dict[str, Any]:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        error["data"] = exc.data
    body: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if mid is not None:
        body["id"] = mid
    return body


def _dump(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ------------------------------------------------------------------ stdio


def serve_stdio(server: Server) -> None:
    out = sys.stdout.buffer
    if "CONF-015" in server.breaks:
        out.write(b"reference server ready\n")
        out.flush()
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        _, response = server.handle(raw)
        if response is not None:
            out.write(_dump(response) + b"\n")
            out.flush()


# ------------------------------------------------------------------- HTTP

ALLOWED_ORIGIN_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _decode(value: str) -> str:
    if value.startswith("=?base64?") and value.endswith("?="):
        return base64.b64decode(value[9:-2]).decode("utf-8")
    return value


def _origin_allowed(origin: str) -> bool:
    for scheme in ("http://", "https://"):
        if origin.startswith(scheme):
            host = origin[len(scheme) :].split("/")[0]
            host = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return host in ALLOWED_ORIGIN_HOSTS
    return False


def _header_mismatch(server: Server, headers: Any, raw: bytes) -> str | None:
    """Server validation (streamable-http#server-validation)."""
    if "CONF-006" in server.breaks:
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict) or "id" not in message:
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    expected = {
        "MCP-Protocol-Version": meta.get(PV),
        "Mcp-Method": message.get("method"),
    }
    source = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}.get(
        str(message.get("method"))
    )
    if source:
        expected["Mcp-Name"] = params.get(source)
    for name, body_value in expected.items():
        if body_value is None:
            continue
        header = headers.get(name)
        if header is None:
            return f"missing required header {name}"
        if _decode(header) != body_value:
            return f"{name} header value {header!r} does not match body value {body_value!r}"
    return None


def make_http_server(server: Server, port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, status: int, body: dict[str, Any] | None) -> None:
            data = b"" if body is None else _dump(body)
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(405, None)

        do_DELETE = do_GET  # noqa: N815 - http.server's method naming

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            origin = self.headers.get("Origin")
            if (
                origin is not None
                and "CONF-007" not in server.breaks
                and not _origin_allowed(origin)
            ):
                self._send(403, _error(None, Rpc(-32600, "Forbidden origin")))
                return
            mismatch = _header_mismatch(server, self.headers, raw)
            if mismatch is not None:
                try:
                    mid = json.loads(raw).get("id")
                except ValueError:
                    mid = None
                self._send(400, _error(mid, Rpc(-32020, f"Header mismatch: {mismatch}")))
                return
            status, response = server.handle(raw)
            self._send(status, response)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--break",
        dest="breaks",
        action="append",
        default=[],
        choices=sorted(BREAKS),
        help="violate this check",
    )
    parser.add_argument("--http", type=int, metavar="PORT", help="serve HTTP instead of stdio")
    args = parser.parse_args(argv)
    server = Server(set(args.breaks))
    if args.http is None:
        serve_stdio(server)
        return
    httpd = make_http_server(server, args.http)
    sys.stderr.write(f"http://127.0.0.1:{httpd.server_address[1]}/mcp\n")
    sys.stderr.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
