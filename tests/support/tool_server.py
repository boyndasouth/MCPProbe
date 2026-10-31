"""A minimal modern MCP server with one configurable tool, for FUZZ edge cases.

    python tool_server.py hang         # `slow` hangs on any non-empty text
    python tool_server.py drop         # `slow` never answers non-empty text, but
                                       # keeps answering everything else
    python tool_server.py progress     # `slow` takes 2.5 s, reporting progress
    python tool_server.py schema FILE  # `probe` uses the inputSchema in FILE and
                                       # answers every call with a result
    python tool_server.py annotated    # tools with openWorld / unannotated hints

Not a fixture for a check; the reference server is. Standard library only.
"""

import json
import sys
import time
from pathlib import Path

MODE = sys.argv[1]
TOOLS: list[dict[str, object]]
if MODE in ("hang", "drop", "progress"):
    TOOLS = [
        {
            "name": "slow",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True},
        }
    ]
elif MODE == "schema":
    schema = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    TOOLS = [{"name": "probe", "inputSchema": schema, "annotations": {"readOnlyHint": True}}]
else:
    TOOLS = [
        {
            "name": "web",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True, "openWorldHint": True},
        },
        {"name": "plain", "inputSchema": {"type": "object"}},
    ]

META = {"io.modelcontextprotocol/serverInfo": {"name": "tool-server", "version": "1"}}


def write(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def result(mid: object, body: dict[str, object]) -> None:
    write({"jsonrpc": "2.0", "id": mid, "result": {"resultType": "complete", **body}})


for raw in sys.stdin:
    message = json.loads(raw)
    mid, method = message.get("id"), message.get("method")
    params = message.get("params") or {}
    if mid is None:
        continue
    if method == "server/discover":
        result(mid, {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}})
    elif method == "tools/list":
        result(mid, {"tools": TOOLS})
    elif method == "tools/call":
        arguments = params.get("arguments")
        busy = isinstance(arguments, dict) and bool(arguments.get("text"))
        if MODE == "hang" and busy:
            time.sleep(3600)
        if MODE == "drop" and busy:
            continue
        valid = (
            isinstance(arguments, dict)
            and set(arguments) == {"text"}
            and isinstance(arguments["text"], str)
        )
        if MODE == "progress" and not valid:
            result(mid, {"content": [{"type": "text", "text": "bad arguments"}], "isError": True})
            continue
        if MODE == "progress":
            token = (params.get("_meta") or {}).get("progressToken")
            for step in range(5):
                time.sleep(0.5)
                if token is not None:
                    write(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/progress",
                            "params": {"progressToken": token, "progress": step + 1},
                        }
                    )
        result(mid, {"content": [{"type": "text", "text": "ok"}]})
    else:
        write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
