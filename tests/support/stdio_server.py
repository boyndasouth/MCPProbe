"""Deterministic stdio test server for transport tests. Not an MCP server.

Reads newline-delimited JSON and answers by ``method``:
  echo      -> result {"echo": params}
  notify    -> a notification line, then the result
  secret    -> result containing $PROBE_SECRET
  log       -> a stderr line, then the result
  crash     -> writes half a line to stdout and exits with code 3
  big       -> result with a params["size"]-byte string
  grandchild-> spawns a long-sleeping grandchild, prints its pid
  silent    -> no answer
Anything unparsable gets an error response with id null.
"""

import json
import os
import subprocess
import sys


def write(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    try:
        msg = json.loads(raw)
    except ValueError:
        write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        continue
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params")
    if method == "crash":
        sys.stdout.write('{"partial":')
        sys.stdout.flush()
        sys.exit(3)
    if method == "silent":
        continue
    if method == "notify":
        write({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"n": 1}})
    if method == "log":
        sys.stderr.write("server log line\n")
        sys.stderr.flush()
    if method == "big":
        result: object = {"data": "x" * int(params["size"])}
    elif method == "secret":
        result = {"secret": os.environ.get("PROBE_SECRET", "")}
    elif method == "grandchild":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        result = {"pid": child.pid}
    else:
        result = {"echo": params}
    if mid is not None:
        write({"jsonrpc": "2.0", "id": mid, "result": result})
