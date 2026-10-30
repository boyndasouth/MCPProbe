"""Machine-readable report. Schema-versioned and deterministic.

Same server + same seed + same MCPProbe version gives byte-identical output:
no timestamps, no durations, results sorted by case id, keys in fixed order.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcpprobe import __version__
from mcpprobe.engine.connection import HttpServer
from mcpprobe.engine.runner import Status

if TYPE_CHECKING:
    from mcpprobe.engine.connection import ServerSpec
    from mcpprobe.engine.runner import RunResult

REPORT_FORMAT = "mcpprobe.report"
REPORT_VERSION = 1


def _server(spec: ServerSpec) -> dict[str, Any]:
    if isinstance(spec, HttpServer):
        return {"transport": "http", "url": spec.url}
    return {"transport": "stdio", "argv": list(spec.argv), "env_keys": sorted(spec.env)}


def build(result: RunResult, spec: ServerSpec) -> dict[str, Any]:
    facts = result.facts
    return {
        "format": REPORT_FORMAT,
        "version": REPORT_VERSION,
        "tool": {"name": "mcpprobe", "version": __version__},
        "seed": result.seed,
        "server": _server(spec),
        "era": None if facts is None else facts.era.value,
        "exit_code": int(result.exit_code),
        "summary": {status.value: result.count(status) for status in Status},
        "restarts": result.restarts,
        "stopped_early": result.stopped_early,
        "results": [
            {
                "id": r.case_id,
                "suite": r.suite.value,
                "severity": r.severity.value,
                "status": r.status.value,
                "message": r.message,
                "spec_ref": r.spec_ref,
                "repro": r.repro,
            }
            for r in sorted(result.results, key=lambda r: r.case_id)
        ],
    }


def dumps(result: RunResult, spec: ServerSpec) -> str:
    return json.dumps(build(result, spec), indent=2, ensure_ascii=False) + "\n"
