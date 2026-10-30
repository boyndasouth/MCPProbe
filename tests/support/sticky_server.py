"""Dual-era test server that locks into legacy mode on the first request
without modern _meta, the way real dual-era servers can on stdio."""

import json
import sys

legacy = False
for raw in sys.stdin:
    msg = json.loads(raw)
    meta = (msg.get("params") or {}).get("_meta") or {}
    modern = "io.modelcontextprotocol/protocolVersion" in meta
    if not modern:
        legacy = True
    mid = msg.get("id")
    if msg.get("method") == "server/discover" and modern and not legacy:
        result = {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {},
            "ttlMs": 0,
            "cacheScope": "public",
        }
        reply = {"jsonrpc": "2.0", "id": mid, "result": result}
    elif msg.get("method") == "server/discover":
        reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}}
    else:
        result = {"tools": []}
        if not legacy:
            result["resultType"] = "complete"
        reply = {"jsonrpc": "2.0", "id": mid, "result": result}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
