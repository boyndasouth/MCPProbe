"""Reference MCP server (2026-07-28 only) for MCPProbe's own tests.

Standard library only, written against the raw wire protocol so it can break
the protocol on purpose. Run with no flags it passes every implemented check;
``--break CONF-NNN`` injects exactly the violation that check looks for, which
gives each check its failing ("bad") and passing ("good") fixture.

    python reference.py                          # stdio, conformant
    python reference.py --break CONF-004         # stdio, results lack resultType
    python reference.py --http 0                 # HTTP on a free port (printed on stderr)
    python reference.py --era legacy             # initialize-era only (2025-11-25, 2025-06-18)
    python reference.py --era dual               # both eras; legacy sessions per process/session

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
LEGACY_SUPPORTED = ["2025-11-25", "2025-06-18"]
SESSION_HEADER = "Mcp-Session-Id"
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
    "CONF-008": "unknown tools get a tool result with isError: true",
    "CONF-009": "ask_name sends an elicitation input request the client never declared",
    "CONF-010": "ask_name's input_required result has neither inputRequests nor requestState",
    "CONF-011": "weather returns structuredContent that does not match its outputSchema",
    "CONF-013": "InitializeResult lacks serverInfo, and an unsupported version is agreed to",
    "CONF-014": "server/discover lists 2024-11-05, which initialize does not support",
    "CONF-021": "initialize answers an unsupported version with an error",
    "CONF-022": "only the outdated 2025-03-26 revision is offered",
    "FUZZ-001": "echo crashes on a message with a non-ASCII character",
    "FUZZ-002": "invalid tool arguments get -32603 Internal error",
    "FUZZ-003": "echo accepts a message of any type",
    "FUZZ-004": "echo rejects an empty message with -32602, although its schema allows it",
    "ROB-001": "malformed JSON is dropped without an answer",
    "ROB-002": 'requests without jsonrpc: "2.0" are executed anyway',
    "ROB-003": "unknown methods get -32603 Internal error",
    "ROB-004": "tools/call with arguments that are not an object gets -32603",
    "ROB-005": "messages over 2 MB are dropped without an answer",
    "ROB-006": "notifications/cancelled is answered with an error",
    "ROB-007": "unknown-method errors carry the id of the previous request",
}
MAX_MESSAGE = 2_000_000
ERAS = ("modern", "legacy", "dual")

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


class Crash(Exception):
    """Simulated crash: stdio exits; HTTP drops the request without an answer."""


class Drop(Exception):
    """Simulated lost message: stdio writes nothing; HTTP closes the connection."""


class Rpc(Exception):
    def __init__(self, code: int, message: str, data: Any = None, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.data, self.status = code, message, data, status


class Server:
    def __init__(self, breaks: set[str], era: str = "modern") -> None:
        self.breaks = breaks
        self.era = era
        self.list_calls = 0
        self.remembered_version: str | None = None
        self.lock = threading.Lock()
        self.sessions: dict[str, str] = {}
        """Legacy sessions: id -> negotiated version ("stdio" for the process)."""
        self.session_counter = 0
        self.previous_id: Any = None

    @property
    def legacy_supported(self) -> list[str]:
        return ["2025-03-26"] if "CONF-022" in self.breaks else list(LEGACY_SUPPORTED)

    @property
    def advertised_versions(self) -> list[str]:
        """supportedVersions in server/discover."""
        if self.era != "dual":
            return list(SUPPORTED)
        versions = SUPPORTED + self.legacy_supported
        if "CONF-014" in self.breaks:
            versions.append("2024-11-05")
        return versions

    # ------------------------------------------------------------ results

    def _result(
        self, body: dict[str, Any], *, cacheable: bool = False, legacy: bool = False
    ) -> dict[str, Any]:
        if legacy:
            return body  # initialize-era results carry none of the modern fields
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

    def dispatch(
        self, method: str, params: dict[str, Any], *, legacy: bool = False
    ) -> dict[str, Any]:
        if legacy and method == "ping":
            return {}
        if not legacy and method == "server/discover" and "CONF-001" not in self.breaks:
            caps: dict[str, Any] = {"tools": {}}
            if "CONF-016" in self.breaks:
                caps["logging"] = {}
            return self._result(
                {"supportedVersions": self.advertised_versions, "capabilities": caps},
                cacheable=True,
            )
        if method == "tools/list":
            # ask_name needs multi round-trip requests, which legacy sessions lack.
            tools = [t for t in TOOLS if not (legacy and t["name"] == "ask_name")]
            with self.lock:
                self.list_calls += 1
                if "CONF-012" in self.breaks and self.list_calls % 2 == 0:
                    tools.reverse()
            return self._result({"tools": tools}, cacheable=True, legacy=legacy)
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if "ROB-004" in self.breaks and not isinstance(arguments, dict):
                raise Rpc(
                    -32603, "Internal error: 'list' object has no attribute 'get'", status=500
                )
            problem = None if tool is None else self._invalid(tool, arguments)
            if problem is not None:
                if "FUZZ-002" in self.breaks:
                    raise Rpc(-32603, "Internal error", status=500)
                # tools#error-handling: input validation errors are tool execution errors.
                text = f"Invalid arguments for {name}: {problem}"
                return self._result(
                    {"content": [{"type": "text", "text": text}], "isError": True}, legacy=legacy
                )
            if name == "echo":
                message = arguments.get("message", "")
                if "FUZZ-001" in self.breaks and isinstance(message, str) and not message.isascii():
                    raise Crash
                if "FUZZ-004" in self.breaks and message == "":
                    raise Rpc(-32602, "message must not be empty")
                text = str(arguments.get("message", ""))
                return self._result({"content": [{"type": "text", "text": text}]}, legacy=legacy)
            if name == "get_time":
                return self._result({"content": [{"type": "text", "text": "12:00"}]}, legacy=legacy)
            if name == "ask_name" and not legacy:
                return self._ask_name(params)
            if name == "weather":
                city = str(arguments.get("city", "Austin"))
                data: dict[str, Any] = {"city": city, "celsius": 21.5}
                if "CONF-011" in self.breaks:
                    data["celsius"] = "warm"
                text = json.dumps(data)
                return self._result(
                    {"content": [{"type": "text", "text": text}], "structuredContent": data},
                    legacy=legacy,
                )
            if name == "delete_everything":
                sys.stderr.write("delete_everything was called\n")
                sys.stderr.flush()
                raise SystemExit(99)
            if "CONF-008" in self.breaks:
                text = f"Unknown tool: {name}"
                return self._result(
                    {"content": [{"type": "text", "text": text}], "isError": True}, legacy=legacy
                )
            raise Rpc(-32602, f"Unknown tool: {name}", status=200 if legacy else 400)
        # A legacy server answers unknown methods like most 2025-era SDKs: HTTP 200
        # with a JSON-RPC error. A modern one uses 404 (streamable-http).
        if "ROB-003" in self.breaks:
            raise Rpc(-32603, "Internal error", status=500)
        raise Rpc(-32601, "Method not found", status=200 if legacy else 404)

    # ------------------------------------------------------------ legacy era

    def _invalid(self, tool: dict[str, Any], arguments: Any) -> str | None:
        """Why ``arguments`` fail the tool's inputSchema (the subset these tools use)."""
        if not isinstance(arguments, dict):
            return "arguments must be an object"
        schema = tool["inputSchema"]
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in arguments:
                return f"missing required property {name!r}"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(arguments) - set(properties))
            if extra:
                return f"unexpected property {extra[0]!r}"
        lax = tool["name"] == "echo" and "FUZZ-003" in self.breaks
        for name, value in arguments.items():
            rule = properties.get(name, {})
            if rule.get("type") == "string" and not isinstance(value, str) and not lax:
                return f"{name!r} must be a string"
            if "enum" in rule and value not in rule["enum"]:
                return f"{name!r} must be one of {rule['enum']}"
        return None

    def _initialize(self, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """2025-11-25 lifecycle: answer with the requested version if supported,
        otherwise with another version this server supports."""
        requested = params.get("protocolVersion")
        supported = self.legacy_supported
        if requested in supported:
            negotiated = str(requested)
        elif "CONF-013" in self.breaks and isinstance(requested, str):
            negotiated = requested  # agrees to a version it cannot speak
        elif "CONF-021" in self.breaks:
            raise Rpc(
                -32602,
                "Unsupported protocol version",
                {"supported": supported, "requested": requested},
                status=200,
            )
        else:
            negotiated = supported[0]
        result: dict[str, Any] = {"protocolVersion": negotiated, "capabilities": {"tools": {}}}
        if "CONF-013" not in self.breaks:
            result["serverInfo"] = SERVER_INFO
        return result, negotiated

    def _legacy(
        self, method: str, params: dict[str, Any], session: str | None, http: bool
    ) -> tuple[dict[str, Any], str | None]:
        """One legacy request. Returns (result, newly assigned session id)."""
        if method == "initialize":
            result, negotiated = self._initialize(params)
            with self.lock:
                if http:
                    self.session_counter += 1
                    new_id = f"session-{self.session_counter}"
                    self.sessions[new_id] = negotiated
                    return result, new_id
                self.sessions["stdio"] = negotiated
            return result, None
        key = session if http else "stdio"
        if key is None or key not in self.sessions:
            if http and key is not None:
                raise Rpc(-32600, "Unknown or terminated session", status=404)
            raise Rpc(-32600, "Server not initialized", status=400)
        return self.dispatch(method, params, legacy=True), None

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

    def is_modern(self, message: Any) -> bool:
        """Which era a message belongs to, for this server."""
        if self.era == "modern":
            return True
        if self.era == "legacy":
            return False
        params = message.get("params") if isinstance(message, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        return isinstance(meta, dict) and PV in meta

    def handle(
        self, raw: bytes, session: str | None = None, *, http: bool = False
    ) -> tuple[int, dict[str, Any] | None, str | None]:
        """Process one message. Returns (HTTP status, response or None, new session id)."""
        if "ROB-005" in self.breaks and len(raw) > MAX_MESSAGE:
            raise Drop
        try:
            message = json.loads(raw)
        except ValueError:
            if "ROB-001" in self.breaks:
                raise Drop from None
            return 400, _error(None, Rpc(-32700, "Parse error")), None
        lax = "ROB-002" in self.breaks and isinstance(message, dict)
        if not isinstance(message, dict) or (message.get("jsonrpc") != "2.0" and not lax):
            return 400, _error(None, Rpc(-32600, "Invalid Request")), None
        method = message.get("method")
        if not isinstance(method, str):
            return 400, _error(None, Rpc(-32600, "Invalid Request")), None
        if "id" not in message:
            if "ROB-006" in self.breaks and method == "notifications/cancelled":
                return 400, _error(None, Rpc(-32601, "Method not found")), None
            return 202, None, None  # notification: accept, never answer
        mid = message["id"]
        if mid is None or isinstance(mid, (bool, float, list, dict)):
            bad_id = Rpc(-32600, "Invalid Request: id must be a string or integer")
            return 400, _error(None, bad_id), None
        if "CONF-005" in self.breaks and isinstance(mid, int):
            mid = str(mid)
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        new_session: str | None = None
        try:
            if self.is_modern(message):
                self._check_meta(method, params)
                result = self.dispatch(method, params)
            else:
                result, new_session = self._legacy(method, params, session, http)
        except Rpc as exc:
            if exc.code == -32601 and "ROB-007" in self.breaks:
                mid, self.previous_id = self.previous_id, mid  # shared state, wrong answer
            return exc.status, _error(mid, exc), None
        self.previous_id = mid
        return 200, {"jsonrpc": "2.0", "id": mid, "result": result}, new_session


def _error(mid: Any, exc: Rpc) -> dict[str, Any]:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        error["data"] = exc.data
    body: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if mid is not None:
        body["id"] = mid
    return body


def _dump(obj: dict[str, Any]) -> bytes:
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, echoed back: send it escaped
        return json.dumps(obj, separators=(",", ":")).encode("ascii")


# ------------------------------------------------------------------ stdio


def serve_stdio(server: Server) -> None:
    out = sys.stdout.buffer
    if "CONF-015" in server.breaks:
        out.write(b"reference server ready\n")
        out.flush()
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            _, response, _ = server.handle(raw)
        except Crash:
            sys.exit(98)
        except Drop:
            continue
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
    """Server validation (streamable-http#server-validation), modern requests only."""
    if "CONF-006" in server.breaks:
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict) or "id" not in message or not server.is_modern(message):
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
        # Buffer each response so headers and body leave in one write; unbuffered,
        # Nagle's algorithm and delayed ACKs add ~40 ms to every keep-alive request.
        wbufsize = 64 * 1024

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(
            self, status: int, body: dict[str, Any] | None, session: str | None = None
        ) -> None:
            data = b"" if body is None else _dump(body)
            self.send_response(status)
            if session is not None:
                self.send_header(SESSION_HEADER, session)
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
            session = self.headers.get(SESSION_HEADER)
            version = self.headers.get("MCP-Protocol-Version")
            # 2025-11-25 transports#protocol-version-header: invalid or unsupported -> 400.
            known_session = session is not None and session in server.sessions
            if known_session and version is not None and version not in server.legacy_supported:
                error = Rpc(-32600, f"Unsupported MCP-Protocol-Version: {version}")
                self._send(400, _error(None, error))
                return
            try:
                status, response, new_session = server.handle(raw, session, http=True)
            except (Crash, Drop):
                self.close_connection = True  # drop the request: no status line, no body
                return
            self._send(status, response, new_session)

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
    parser.add_argument("--era", choices=ERAS, default="modern", help="protocol era(s) to speak")
    args = parser.parse_args(argv)
    server = Server(set(args.breaks), args.era)
    if args.http is None:
        serve_stdio(server)
        return
    httpd = make_http_server(server, args.http)
    sys.stderr.write(f"http://127.0.0.1:{httpd.server_address[1]}/mcp\n")
    sys.stderr.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
