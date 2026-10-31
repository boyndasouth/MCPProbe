"""``--baseline``: adopt MCPProbe without fixing everything first.

A baseline is a JSON report from an earlier run (``--report json``). Findings
it already contains (matched by case id, which is stable across runs) are
reported but do not fail the run; only new findings do. Case ids, not
messages, are the key: a fuzz finding whose shrunk input changes is still
the same finding.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcpprobe.report.json_report import REPORT_FORMAT

if TYPE_CHECKING:
    from pathlib import Path


class BaselineError(Exception):
    """The baseline file is not an MCPProbe JSON report. Maps to exit code 3."""


FINDING_STATUSES = ("crashed", "timeout")


def _is_finding(item: dict[str, Any]) -> bool:
    status = item.get("status")
    return status in FINDING_STATUSES or (status == "failed" and item.get("severity") == "error")


def load_baseline(path: Path) -> frozenset[str]:
    """Case ids that were findings in the report at ``path``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != REPORT_FORMAT:
        raise BaselineError(
            f"{path} is not an MCPProbe JSON report (make one with --report json -o {path.name})"
        )
    results = data.get("results")
    if not isinstance(results, list):
        raise BaselineError(f"{path} has no results list")
    return frozenset(
        str(item["id"])
        for item in results
        if isinstance(item, dict) and "id" in item and _is_finding(item)
    )
